"""9: recurring-close.sh substantive-artifact probe suppression.

Verifies the second-layer suppression added to the Phase 4.25 enforcement
block in recurring-close.sh. When the empty-WM check (g-115-634) does NOT
suppress (because some buffer is non-empty from earlier work), the new
substantive-artifact probe checks four artifact types in a tight time
window. ALL FOUR negative → suppress the force_experience_archival sentinel.

Probe sources:
  1. Tree node .md file edited in window (world/knowledge/tree/**/*.md mtime)
  2. New goal in world or agent aspirations.jsonl (created_at within window)
  3. Non-status board post by this agent (world/board/*.jsonl)
  4. Pipeline state change (world/pipeline-meta.json mtime)

Tests:
  1. test_forced_flip_with_recent_tree_md_does_not_suppress
  2. test_forced_flip_with_no_artifact_in_window_suppresses
  3. test_forced_flip_with_recent_status_board_post_only_does_not_count

Pattern mirrors test_recurring_close_canary_suppress.py (g-115-634):
extract the canary heredoc body verbatim from recurring-close.sh and run
it via subprocess against a sandboxed AGENT_DIR + WORLD_DIR.

Run: py -3 -m pytest core/scripts/tests/test_recurring_close_cargo_cult_suppression.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"


def _extract_canary_python() -> str:
    """Pull the Phase 4.25 enforcement heredoc body out of recurring-close.sh.

    The block contains BOTH g-115-634 (empty-WM check) AND g-115-1089
    (substantive-artifact probe) — they share the heredoc. We extract the
    whole body so the test exercises the SAME source the production path
    runs (no implementation duplication).
    """
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    sentinel_phrase = "Phase 4.25 enforcement"
    idx = src.find(sentinel_phrase)
    if idx < 0:
        raise RuntimeError(
            f"could not find sentinel '{sentinel_phrase}' in recurring-close.sh"
        )
    here_start = src.find("python3 - <<'PYEOF'", idx)
    body_start = src.find("\n", here_start) + 1
    body_end = src.find("PYEOF", body_start)
    if here_start < 0 or body_end < 0:
        raise RuntimeError("could not locate canary heredoc body")
    return src[body_start:body_end]


CANARY_BODY = _extract_canary_python()


def with_sandbox(test_fn):
    """Sandbox setup: tempdir AGENT_DIR + WORLD_DIR with required structure.

    WM is seeded with NON-empty encoding_queue so the g-115-634 check
    falls through to g-115-1089 (which is what the test exercises).
    """
    def wrapped():
        sandbox = Path(tempfile.mkdtemp(prefix=f"cargo_cult_test_{test_fn.__name__}_"))
        agent_dir = sandbox / "alpha-test"
        (agent_dir / "session").mkdir(parents=True)
        world_dir = sandbox / "world-test"
        world_dir.mkdir()
        (world_dir / "knowledge" / "tree").mkdir(parents=True)
        (world_dir / "board").mkdir()
        meta_dir = sandbox / "meta-test"
        meta_dir.mkdir()

        # Seed WM with non-empty encoding_queue so  does NOT suppress
        # (lets the test reach the 9 probe).
        wm = {"encoding_queue": [{"kind": "test"}], "slots": {"sensory_buffer": [{"event": "x"}]}}
        (agent_dir / "session" / "working-memory.yaml").write_text(
            yaml.safe_dump(wm, sort_keys=False), encoding="utf-8"
        )

        prior_env = {
            k: os.environ.get(k) for k in
            ("MIND_AGENT", "MIND_WORLD", "MIND_META", "MIND_AGENT_DIR",
             "MIND_SID", "GID", "OUTCOME", "ORIGINAL_OUTCOME", "PR", "SD")
        }
        os.environ["MIND_AGENT"] = "alpha-test"
        os.environ["MIND_WORLD"] = str(world_dir)
        os.environ["MIND_META"] = str(meta_dir)
        os.environ["MIND_AGENT_DIR"] = str(agent_dir)
        os.environ.pop("MIND_SID", None)

        try:
            test_fn(sandbox, agent_dir, world_dir)
        finally:
            for k, v in prior_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(sandbox, ignore_errors=True)
    wrapped.__name__ = test_fn.__name__
    return wrapped


def _run_canary(agent_dir: Path, *, gid="g-test", outcome="deep",
                original_outcome="routine"):
    """Invoke the canary heredoc body with env vars matching the prod path."""
    env = os.environ.copy()
    env["GID"] = gid
    env["OUTCOME"] = outcome
    env["ORIGINAL_OUTCOME"] = original_outcome
    env["PR"] = str(PROJECT_ROOT)
    env["SD"] = str(CORE_SCRIPTS)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    return subprocess.run(
        [sys.executable, "-"],
        input=CANARY_BODY,
        capture_output=True, text=True, encoding="utf-8",
        env=env, timeout=20,
    )


@with_sandbox
def test_forced_flip_with_recent_tree_md_does_not_suppress(sandbox, agent_dir, world_dir):
    """Tree .md edited in window → substantive → no 9 suppress.

    Sets mtime of a tree .md to now. The probe should find it and set
    substantive=True, so the g-115-1089 suppression path does NOT fire.
    The g-115-1089 stderr message should NOT appear.
    """
    tree_md = world_dir / "knowledge" / "tree" / "some-node.md"
    tree_md.write_text("# some node\n", encoding="utf-8")
    # mtime is "now" by default — well within the 90s window
    r = _run_canary(agent_dir)
    assert r.returncode == 0, f"non-zero exit: rc={r.returncode} stderr={r.stderr!r}"
    assert "g-115-1089: forced-flip" not in r.stderr, \
        f"g-115-1089 should NOT suppress with recent tree-md, got stderr: {r.stderr!r}"


@with_sandbox
def test_forced_flip_with_no_artifact_in_window_suppresses(sandbox, agent_dir, world_dir):
    """No artifact in window → 9 suppresses with diagnostic.

    No tree edits, no new goals, no board posts, no pipeline-meta. The probe
    finds nothing in the 90s window. g-115-1089 should print the suppression
    message to stderr and exit 0 before reaching the sentinel write.
    """
    # No artifacts seeded — directories exist but are empty
    r = _run_canary(agent_dir)
    assert r.returncode == 0, f"non-zero exit: rc={r.returncode} stderr={r.stderr!r}"
    assert "g-115-1089: forced-flip" in r.stderr, \
        f"expected g-115-1089 suppression message, got stderr: {r.stderr!r}"
    assert "no substantive artifact" in r.stderr, \
        f"expected 'no substantive artifact' reason, got stderr: {r.stderr!r}"


@with_sandbox
def test_forced_flip_with_recent_status_board_post_only_does_not_count(sandbox, agent_dir, world_dir):
    """Status-only board post in window → does NOT qualify as substantive.

    A type=status board post (routine ack/claim/release) is filtered out by
    the probe — only non-status posts count as substantive work. The probe
    should NOT find substantive and should suppress with g-115-1089.
    """
    from datetime import datetime
    now_iso = datetime.now().isoformat()
    board_post = {
        "id": "msg-test-001",
        "author": "alpha-test",
        "type": "status",
        "timestamp": now_iso,
        "text": "test status post",
    }
    coord_jsonl = world_dir / "board" / "coordination.jsonl"
    coord_jsonl.write_text(json.dumps(board_post) + "\n", encoding="utf-8")

    r = _run_canary(agent_dir)
    assert r.returncode == 0, f"non-zero exit: rc={r.returncode} stderr={r.stderr!r}"
    assert "g-115-1089: forced-flip" in r.stderr, \
        f"status-only post should NOT count as substantive — expected g-115-1089 suppression, got stderr: {r.stderr!r}"


if __name__ == "__main__":
    tests = [
        test_forced_flip_with_recent_tree_md_does_not_suppress,
        test_forced_flip_with_no_artifact_in_window_suppresses,
        test_forced_flip_with_recent_status_board_post_only_does_not_count,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"ERROR: {t.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
