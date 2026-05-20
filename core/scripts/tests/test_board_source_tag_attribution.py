"""test_board_source_tag_attribution.py — regression test for the board.py
source-tag attribution loop added by g-115-519.

Pre-fix, fresh-eyes-code findings posted to world/board/findings.jsonl with
tags like [fresh-eyes-code, guard-343, severity:constrains] produced no
positive signal in guard-343's utilization counters. Phase 4.26
utilization-feedback.sh was the only writer to times_helpful, and findings
do not flow through that path. Result: guard-343 showed utilization_score
=0.04 despite producing 59/103 critical fresh-eyes findings (57% share)
across the 246-finding/n=363-suppression-audit window 2026-04-27..05-09.

This test isolates the new attribution loop in board.py cmd_post by:
  1. Building a minimal temp world dir with seeded guardrails.jsonl + reasoning-bank.jsonl.
  2. Setting MIND_WORLD via a temp agent's local-paths.conf so _paths.py
     routes there.
  3. Posting findings via board.py with various tag combinations.
  4. Asserting counter increments fire ONLY for the findings channel,
     ONLY for valid guard-NNN / rb-NNN tag shapes, and exactly once per
     unique source-tag per post.

Self-contained: never touches the live world.
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
sys.path.insert(0, str(CORE_SCRIPTS))


def _seed_jsonl_stores(world_dir: Path) -> None:
    """Write minimal guardrails.jsonl and reasoning-bank.jsonl with one
    fixture each. Counter starts at zero so post-test reads are unambiguous."""
    world_dir.mkdir(parents=True, exist_ok=True)

    guard_fixture = {
        "id": "guard-901",
        "rule": "TEST FIXTURE — for board-source-tag-attribution regression test only",
        "category": "test-fixture",
        "trigger_condition": "test-only",
        "source": "test-fixture",
        "created": "2026-05-09T00:00:00",
        "status": "active",
        "tags": [],
        "utilization": {
            "retrieval_count": 5,
            "last_retrieved": "2026-05-09",
            "times_helpful": 0,
            "times_inferred_helpful": 0,
            "times_noise": 0,
            "times_active": 0,
            "times_skipped": 0,
            "times_inferred_unknown": 0,
            "times_cited": 0,
            "utilization_score": 0.0,
        },
    }
    (world_dir / "guardrails.jsonl").write_text(
        json.dumps(guard_fixture) + "\n", encoding="utf-8"
    )

    rb_fixture = {
        "id": "rb-901",
        "title": "TEST FIXTURE — board-source-tag-attribution regression",
        "type": "user_provided",
        "category": "test-fixture",
        "content": "test-only",
        "created": "2026-05-09T00:00:00",
        "status": "active",
        "when_to_use": {"conditions": [], "category": ""},
        "tags": [],
        "utilization": {
            "retrieval_count": 5,
            "last_retrieved": "2026-05-09",
            "times_helpful": 0,
            "times_inferred_helpful": 0,
            "times_noise": 0,
            "times_active": 0,
            "times_skipped": 0,
            "times_inferred_unknown": 0,
            "times_cited": 0,
            "utilization_score": 0.0,
        },
    }
    (world_dir / "reasoning-bank.jsonl").write_text(
        json.dumps(rb_fixture) + "\n", encoding="utf-8"
    )


def _seed_minimal_paths_conf(repo_root: Path, world_dir: Path, meta_dir: Path) -> Path:
    """Create a temp agent dir with local-paths.conf so _paths.py resolves
    WORLD_DIR/META_DIR to our scratch dirs."""
    agent_dir = repo_root / "test-board-source-tag-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    conf_text = (
        "# auto-generated test config — safe to delete\n"
        f"WORLD_PATH={world_dir.as_posix()}\n"
        f"META_PATH={meta_dir.as_posix()}\n"
    )
    (agent_dir / "local-paths.conf").write_text(conf_text, encoding="utf-8")
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    return agent_dir


def _read_record(jsonl_path: Path, rec_id: str) -> dict | None:
    if not jsonl_path.exists():
        return None
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            rec = json.loads(stripped)
            if rec.get("id") == rec_id:
                return rec
    return None


def _post(env: dict, world_dir: Path, channel: str, tags: str, text: str) -> int:
    """Invoke board.py post via subprocess. Returns rc."""
    board_script = CORE_SCRIPTS / "board.py"
    proc = subprocess.run(
        [sys.executable, str(board_script), "post",
         "--channel", channel, "--tags", tags, "--author", "test-agent"],
        input=text,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=30,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[board.post stderr]\n{proc.stderr}\n")
    return proc.returncode


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="board-source-tag-test-"))
    world_dir = tmp / "world"
    meta_dir = tmp / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    _seed_jsonl_stores(world_dir)

    agent_dir = None
    failed = []
    try:
        agent_dir = _seed_minimal_paths_conf(PROJECT_ROOT, world_dir, meta_dir)
        env = os.environ.copy()
        env["MIND_AGENT"] = agent_dir.name
        env["MIND_WORLD"] = world_dir.as_posix()
        env["MIND_META"] = meta_dir.as_posix()

        guard_path = world_dir / "guardrails.jsonl"
        rb_path = world_dir / "reasoning-bank.jsonl"

        # ─── Case 1: findings channel + valid guard-NNN tag → increments ───
        rc = _post(env, world_dir, "findings",
                   "fresh-eyes-code,guard-901,severity:constrains",
                   "Test finding 1 — should attribute to guard-901.")
        if rc != 0:
            failed.append(f"Case 1: post failed with rc={rc}")
        guard = _read_record(guard_path, "guard-901")
        if not guard:
            failed.append("Case 1: guard-901 record missing")
        else:
            tih = guard.get("utilization", {}).get("times_inferred_helpful", -1)
            if tih != 1:
                failed.append(f"Case 1: guard-901 times_inferred_helpful expected 1, got {tih}")

        # ─── Case 2: findings channel + valid rb-NNN tag → increments ───
        rc = _post(env, world_dir, "findings",
                   "fresh-eyes-code,rb-901,severity:informs",
                   "Test finding 2 — should attribute to rb-901.")
        if rc != 0:
            failed.append(f"Case 2: post failed with rc={rc}")
        rb = _read_record(rb_path, "rb-901")
        if not rb:
            failed.append("Case 2: rb-901 record missing")
        else:
            tih = rb.get("utilization", {}).get("times_inferred_helpful", -1)
            if tih != 1:
                failed.append(f"Case 2: rb-901 times_inferred_helpful expected 1, got {tih}")

        # ─── Case 3: findings channel + multiple unique source tags ───
        # Each unique tag should increment exactly once. After this post,
        # guard-901 should be at 2 and rb-901 should be at 2.
        rc = _post(env, world_dir, "findings",
                   "fresh-eyes-code,guard-901,rb-901,severity:invalidates",
                   "Test finding 3 — multi-source attribution.")
        if rc != 0:
            failed.append(f"Case 3: post failed with rc={rc}")
        guard = _read_record(guard_path, "guard-901")
        rb = _read_record(rb_path, "rb-901")
        if guard and guard.get("utilization", {}).get("times_inferred_helpful") != 2:
            failed.append(f"Case 3: guard-901 expected 2, got "
                          f"{guard.get('utilization', {}).get('times_inferred_helpful')}")
        if rb and rb.get("utilization", {}).get("times_inferred_helpful") != 2:
            failed.append(f"Case 3: rb-901 expected 2, got "
                          f"{rb.get('utilization', {}).get('times_inferred_helpful')}")

        # ─── Case 4: findings channel + non-source tags only → no increments ───
        # severity:constrains and affects:foo are not source-tags.
        rc = _post(env, world_dir, "findings",
                   "fresh-eyes-code,severity:constrains,affects:core/scripts/foo.sh",
                   "Test finding 4 — no source-tags, no attribution.")
        if rc != 0:
            failed.append(f"Case 4: post failed with rc={rc}")
        guard = _read_record(guard_path, "guard-901")
        rb = _read_record(rb_path, "rb-901")
        # Counters should be UNCHANGED from Case 3 (still 2 each).
        if guard and guard.get("utilization", {}).get("times_inferred_helpful") != 2:
            failed.append(f"Case 4: guard-901 should stay at 2, got "
                          f"{guard.get('utilization', {}).get('times_inferred_helpful')}")
        if rb and rb.get("utilization", {}).get("times_inferred_helpful") != 2:
            failed.append(f"Case 4: rb-901 should stay at 2, got "
                          f"{rb.get('utilization', {}).get('times_inferred_helpful')}")

        # ─── Case 5: NON-findings channel + source tag → NO increment ───
        # Only the findings channel triggers attribution. A guard-NNN tag on
        # a coordination/general/decisions post is informational, not a
        # value-signal.
        rc = _post(env, world_dir, "general",
                   "guard-901,note",
                   "Test finding 5 — guard-tagged general post should NOT attribute.")
        if rc != 0:
            failed.append(f"Case 5: post failed with rc={rc}")
        guard = _read_record(guard_path, "guard-901")
        # Still 2 from Case 3.
        if guard and guard.get("utilization", {}).get("times_inferred_helpful") != 2:
            failed.append(f"Case 5: guard-901 should stay at 2 (non-findings channel), got "
                          f"{guard.get('utilization', {}).get('times_inferred_helpful')}")

        # ─── Case 6: utilization_score recompute ───
        # After the increments above, guard-901 should have:
        #   times_helpful=0, times_inferred_helpful=2, retrieval_count=5
        #   utilization_score = (0 + 0.5*2) / 5 = 0.2
        guard = _read_record(guard_path, "guard-901")
        if guard:
            score = guard.get("utilization", {}).get("utilization_score")
            expected = round((0 + 0.5 * 2) / 5, 4)
            if score != expected:
                failed.append(f"Case 6: guard-901 utilization_score expected {expected}, got {score}")

        # ─── Report ───
        if failed:
            print("\n".join(f"FAIL: {f}" for f in failed), file=sys.stderr)
            return 1
        print("PASS: all 6 cases")
        return 0

    finally:
        if agent_dir and agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
