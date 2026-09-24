"""test_inferred_unknown_autoflag.py — C.3 unknown-counter + auto-flag.

Verifies that:
  * --all-unknown bumps utilization.times_inferred_unknown for every
    supplementary item (not just legacy --all-noise → times_noise pollution).
  * Once times_inferred_unknown reaches unknown_threshold, the record's
    top-level auto_flagged_for_review flips to true.
  * B.1's utilization-stats.py picks up auto_flagged records as candidates
    regardless of their evidence/age.

Self-contained: tmpdir world + agent + memory-pipeline.yaml override.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SCRIPT_FEEDBACK = CORE_SCRIPTS / "utilization-feedback.py"
SCRIPT_STATS = CORE_SCRIPTS / "utilization-stats.py"
sys.path.insert(0, str(CORE_SCRIPTS))

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402
from _utilization_store import SPOOLED_ENV as SPOOL_FLAG  # noqa: E402


@contextlib.contextmanager
def _spool_off():
    """Pin the  counter spool OFF: for the in-process fixture daemon,
    which reads this environment at request time, and for the children that
    inherit it. Restored afterwards so a box's real posture leaks nowhere."""
    prior = os.environ.get(SPOOL_FLAG)
    os.environ[SPOOL_FLAG] = "0"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(SPOOL_FLAG, None)
        else:
            os.environ[SPOOL_FLAG] = prior


def _seed_guard(world, gid):
    g = {
        "id": gid,
        "rule": "test", "category": "test",
        "trigger_condition": "test", "source": "test",
        "status": "active", "created": "2026-01-01",
        "utilization": {
            "retrieval_count": 100, "times_helpful": 0,
            "times_inferred_helpful": 0, "times_active": 0,
            "times_cited": 0, "times_skipped": 50, "times_noise": 0,
            "times_inferred_unknown": 0,
            "utilization_score": 0.0, "last_retrieved": "",
        },
    }
    return g


def _seed_session(agent, goal_id, supp_items):
    """retrieval-session.json with supp_items in tokens that won't match
    anything (force into noise → unknown after threshold check)."""
    session = {
        "schema_version": 2,
        "goal_id": goal_id,
        "timestamp": "2026-05-09T12:00:00",
        "categories": ["test"],
        "tree_nodes_loaded": [],
        "tree_nodes_detail": [],
        "supplementary_items": supp_items,
        "supplementary_detail": [
            {"id": s["id"], "type": s["type"],
             "summary": "test", "distinctive_tokens": [],
             "times_active_at_retrieve": 0}
            for s in supp_items
        ],
        "utilization_pending": True,
    }
    sess_dir = agent / "session"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "retrieval-session.json").write_text(
        json.dumps(session), encoding="utf-8"
    )


def test_all_unknown_increments_inferred_unknown_and_autoflags_at_threshold():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = tmp / "world"
        agent = tmp / "alpha"
        world.mkdir()
        agent.mkdir()
        (agent / "aspirations.jsonl").write_text("", encoding="utf-8")

        guard = _seed_guard(world, "guard-9001")
        with open(world / "guardrails.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(guard) + "\n")
        (world / "reasoning-bank.jsonl").write_text("", encoding="utf-8")
        (world / "aspirations.jsonl").write_text("", encoding="utf-8")

        # This test asserts the LEGACY in-record increment. Since  a box
        # whose environment carries UTILIZATION_COUNTERS_SPOOLED=1 -- every fleet
        # box, from .claude/settings.json env, so every run from inside a Claude
        # session -- routes the increment to the counter SPOOL instead: the daemon
        # answers 200 {"spooled": true}, the embedded field stays 0, and this
        # test read "expected 5, got 0" on every box since the cutover while
        # passing in a bare shell (). The fixture daemon runs
        # IN-PROCESS and reads this process's environment at request time, so
        # the flag is pinned off for both it and the children below. The
        # embedded-block read further down is right ONLY on this legacy path:
        # on a cut-over box that block is a frozen pre-split snapshot (guard-4956)
        # and a spooled increment is invisible to an immediate read-back by
        # design (guard-4631) -- the spooled path needs its own test.
        with _spool_off(), DaemonFixture(world, agent_dir=agent) as df:
            env = {
                **os.environ,
                "MIND_WORLD": str(world),
                "MIND_AGENT_DIR": str(agent),
            }
            # Daemon store WRITES require the X-Mind-Agent header (else 400
            # missing_agent_header — mycelium store-write hardening). _rt derives
            # that header from MIND_AGENT, so keep it set to the fixture's agent.
            # Session reads stay isolated to the tmp agent dir via MIND_AGENT_DIR
            # precedence (_paths.py:222). Popping MIND_AGENT here silently dropped
            # every increment (the daemon 400'd, feedback fail-softs, iu stuck at 0).
            env["MIND_AGENT"] = "alpha"

            # Threshold defaults to 5; run --all-unknown 5 times to cross.
            for i in range(5):
                goal_id = f"g-test-c3-{i:03d}"
                _seed_session(agent, goal_id, [
                    {"id": "guard-9001", "type": "guardrail"}
                ])
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT_FEEDBACK),
                     "--goal", goal_id, "--all-unknown"],
                    env=env, capture_output=True, text=True, encoding="utf-8",
                )
                assert proc.returncode == 0, (
                    f"feedback iter {i} failed: {proc.stdout}\n{proc.stderr}"
                )

            # Live record should now have times_inferred_unknown=5 AND
            # auto_flagged_for_review=true.
            with open(world / "guardrails.jsonl", "r", encoding="utf-8") as f:
                rec = json.loads(f.readline())

            iu = rec["utilization"].get("times_inferred_unknown", 0)
            flagged = rec.get("auto_flagged_for_review", False)
            assert iu == 5, f"expected times_inferred_unknown=5, got {iu}"
            assert flagged is True or flagged == "true", (
                f"auto_flagged_for_review should be true after 5 unknowns, got {flagged!r}"
            )

            # B.1 candidate filter should now include this guardrail despite
            # zero exposure history — auto_flagged forces inclusion.
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_STATS),
                 "guardrails", "candidates", "--limit", "10"],
                env=env, capture_output=True, text=True, encoding="utf-8",
            )
            assert proc.returncode == 0, (
                f"utilization-stats failed: {proc.stdout}\n{proc.stderr}"
            )
            out = json.loads(proc.stdout)
            ids = {it["id"] for it in out["items"]}
            assert "guard-9001" in ids, (
                f"auto-flagged guard not in candidate list: {ids}"
            )


if __name__ == "__main__":
    test_all_unknown_increments_inferred_unknown_and_autoflags_at_threshold()
    print("PASS: --all-unknown bumps times_inferred_unknown and auto-flags at threshold")
