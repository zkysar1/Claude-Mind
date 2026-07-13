"""test_board_write_durability.py — regression guard for board-post durability
(g-328-30).

Incident (ZDS prod 2026-07-07): a board POST returned 200 + a minted msg-id in
16ms but persisted NOWHERE (grep .mind-data = 0 hits; general.jsonl mtime
unchanged). Normal writes take ~1s. Root shape: ACK-before-persist — the id was
minted + returned before the durable append, and the un-flushed write was lost
at a daemon restart.

The durable-before-ACK guarantee this test pins: board.py cmd_post mints the id
INSIDE the locked append (locked_append_jsonl_with_allocator) and prints it only
AFTER the append returns (board.py:127-145). So a printed id IMPLIES the record
is durably on disk. The daemon endpoint board_write.py::post replicates the same
ordering (append via get_backend().append_jsonl_record INSIDE file_locks.locked,
Response 200 returned only after — mind_api/src/endpoints/board_write.py:181-228).

This test guards the CLI write path (board.py cmd_post): post a message, capture
the printed id, then read that id back FROM THE PERSISTED STORE and verify the
record is complete. A regression to the ACK-before-persist shape (print id
before the append) would make the read-back fail — which is exactly the
g-328-30 failure mode. Reading the store directly is the self-contained
read-back (board.py has no `read` subcommand — the read path is the daemon
endpoint / board-read.sh, which needs a live daemon); the persisted store IS
the durability surface the incident violated. Implements the goal's suggested
reproduction check (post -> read-back -> assert id present) as a standing
regression.

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


def _seed_paths_conf(repo_root: Path, world_dir: Path, meta_dir: Path) -> Path:
    """Temp agent dir with local-paths.conf so _paths.py routes WORLD/META to
    our scratch dirs (mirrors test_board_source_tag_attribution.py)."""
    agent_dir = repo_root / "test-board-durability-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "local-paths.conf").write_text(
        "# auto-generated test config — safe to delete\n"
        f"WORLD_PATH={world_dir.as_posix()}\n"
        f"META_PATH={meta_dir.as_posix()}\n",
        encoding="utf-8",
    )
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    return agent_dir


def _post_capture_id(env: dict, channel: str, text: str, tags: str = "") -> tuple[int, str]:
    """Invoke `board.py post`; return (rc, printed_id). cmd_post prints the id to
    stdout AFTER the locked append — durable-before-ACK."""
    board_script = CORE_SCRIPTS / "board.py"
    proc = subprocess.run(
        [sys.executable, str(board_script), "post",
         "--channel", channel, "--tags", tags, "--author", "test-agent"],
        input=text, capture_output=True, text=True, env=env,
        cwd=str(PROJECT_ROOT), timeout=30,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[board.post stderr]\n{proc.stderr}\n")
    return proc.returncode, proc.stdout.strip()


def _read_record(jsonl_path: Path, rec_id: str) -> "dict | None":
    """Read the persisted record back by id from the channel store — the durable
    read-back. Returns the parsed record or None. (board.py has no `read`
    subcommand; the read path is the daemon endpoint / board-read.sh. Reading the
    persisted store directly is the self-contained way to assert the ACK'd write
    is durably retrievable — which is exactly the durable-before-ACK guarantee.)"""
    if not jsonl_path.exists():
        return None
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and json.loads(s).get("id") == rec_id:
                return json.loads(s)
    return None


def _run_case(env: dict, world_dir: Path, channel: str, note: str, tags: str = "") -> list[str]:
    """Post, then read the ACK'd id back from the persisted store and verify the
    record is complete. Returns failure strings.

    Durable-before-ACK: the id printed by cmd_post (the ACK) MUST be a complete,
    retrievable record on disk. The g-328-30 incident violated this (ACK'd id,
    zero persistence)."""
    failed: list[str] = []
    text = f"durability probe ({note}) — an ACK'd id must be persisted + retrievable"
    rc, printed_id = _post_capture_id(env, channel, text, tags)
    if rc != 0:
        return [f"{channel}: post rc={rc}"]
    if not printed_id or not printed_id.startswith("msg-"):
        return [f"{channel}: post returned no msg-id (got {printed_id!r})"]
    # Read the ACK'd id back from the persisted store (durable-before-ACK).
    store = world_dir / "board" / f"{channel}.jsonl"
    rec = _read_record(store, printed_id)
    if rec is None:
        failed.append(
            f"{channel}: ACK'd id {printed_id} NOT persisted/retrievable in "
            f"{store.name} — ACK-before-persist regression (the g-328-30 failure mode)")
    elif rec.get("text") != text:
        failed.append(
            f"{channel}: persisted record {printed_id} text mismatch "
            f"(got {rec.get('text')!r}) — partial/corrupt write")
    return failed


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="board-durability-test-"))
    world_dir = tmp / "world"
    meta_dir = tmp / "meta"
    (world_dir / "board").mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    agent_dir = None
    failed: list[str] = []
    try:
        agent_dir = _seed_paths_conf(PROJECT_ROOT, world_dir, meta_dir)
        env = os.environ.copy()
        env["MIND_AGENT"] = agent_dir.name
        env["MIND_WORLD"] = world_dir.as_posix()
        env["MIND_META"] = meta_dir.as_posix()

        # general — the incident channel (default type=status).
        failed += _run_case(env, world_dir, "general", "general/status")
        # findings — different code path (source-tag side effect after append).
        failed += _run_case(env, world_dir, "findings", "findings", tags="g-328-30")
        # coordination — wake-signal side-effect path after append.
        failed += _run_case(env, world_dir, "coordination", "coordination")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if agent_dir and agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)

    if failed:
        for f in failed:
            print("FAIL:", f)
        return 1
    print("PASS: board post durable-before-ACK (general/findings/coordination: "
          "post -> store persisted -> read-back)")
    return 0


def test_board_write_durability():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
