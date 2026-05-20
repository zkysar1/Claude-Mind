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

        with DaemonFixture(world, agent_dir=agent) as df:
            env = {
                **os.environ,
                "MIND_WORLD": str(world),
                "MIND_AGENT_DIR": str(agent),
            }
            env.pop("MIND_AGENT", None)

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
