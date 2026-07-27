"""test_board_reply_to_warning.py —  regression.

THE GAP (observed 2026-07-21, alpha assistant session): a board reply posted
with a GUESSED msg id (suffix invented) succeeded SILENTLY — board.py cmd_post /
board_write.py did not validate that reply_to referenced an existing message in
the channel, so thread linkage could dangle with no signal.

THE FIX (warn, not block): on post, check the channel's existing records for the
reply_to id; if absent, emit a WARN (to stderr for the CLI twin; to the response
`warnings` array for the daemon path, surfaced to stderr by board-post.sh). It
does NOT block — cross-box lag means the parent may legitimately not be local
yet. Fail-open: any error skips the check.

This test pins the CLI-twin path (board.py cmd_post) via subprocess against a
scratch world (mirrors test_board_write_durability.py):
  1. DANGLING reply_to  -> WARN on stderr AND the post still succeeds (id printed).
  2. VALID reply_to     -> NO warn (reply to a genuinely-posted message).
  3. NO reply_to        -> NO warn (control).

Self-contained: never touches the live world; STORAGE_BACKEND=local pins the
tmp write to the local filesystem (own-cloud S3-key-collision guard).
"""
from __future__ import annotations

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
    agent_dir = repo_root / "test-board-replyto-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "local-paths.conf").write_text(
        "# auto-generated test config — safe to delete\n"
        f"WORLD_PATH={world_dir.as_posix()}\n"
        f"META_PATH={meta_dir.as_posix()}\n",
        encoding="utf-8",
    )
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    return agent_dir


def _post(env: dict, channel: str, text: str, reply_to: str | None = None
          ) -> tuple[int, str, str]:
    """Invoke `board.py post`; return (rc, stdout_id, stderr)."""
    board_script = CORE_SCRIPTS / "board.py"
    cmd = [sys.executable, str(board_script), "post",
           "--channel", channel, "--author", "test-agent"]
    if reply_to is not None:
        cmd += ["--reply-to", reply_to]
    proc = subprocess.run(
        cmd, input=text, capture_output=True, text=True, env=env,
        cwd=str(PROJECT_ROOT), timeout=30,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr


_WARN_MARK = "[board-post] WARN"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="board-replyto-test-"))
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
        env["STORAGE_BACKEND"] = "local"

        # Case 1: DANGLING reply_to -> WARN on stderr, post still succeeds.
        rc, mid, err = _post(env, "general", "reply to a made-up parent",
                             reply_to="msg-20260101-000000-nobody-9999")
        if rc != 0:
            failed.append(f"dangling: post rc={rc} (should still succeed) stderr={err!r}")
        if not mid.startswith("msg-"):
            failed.append(f"dangling: no msg-id printed (got {mid!r}) — WARN must not block the post")
        if _WARN_MARK not in err or "reply_to" not in err:
            failed.append(f"dangling: expected a reply_to WARN on stderr; got stderr={err!r}")

        # Case 2: VALID reply_to -> NO warn. First post a real parent.
        rc0, parent_id, err0 = _post(env, "general", "the real parent message")
        if rc0 != 0 or not parent_id.startswith("msg-"):
            failed.append(f"valid-setup: parent post failed rc={rc0} id={parent_id!r}")
        else:
            rc2, mid2, err2 = _post(env, "general", "a valid reply", reply_to=parent_id)
            if rc2 != 0 or not mid2.startswith("msg-"):
                failed.append(f"valid: reply post failed rc={rc2} id={mid2!r}")
            if _WARN_MARK in err2:
                failed.append(f"valid: reply to an EXISTING id must NOT warn; got stderr={err2!r}")

        # Case 3: NO reply_to -> NO warn (control).
        rc3, mid3, err3 = _post(env, "general", "a plain post, no reply")
        if _WARN_MARK in err3:
            failed.append(f"no-reply: a post without reply_to must NOT warn; got stderr={err3!r}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if agent_dir and agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)

    if failed:
        for f in failed:
            print("FAIL:", f)
        return 1
    print("PASS: board reply_to warning (dangling->warn+post-ok / valid->no-warn / none->no-warn)")
    return 0


def test_board_reply_to_warning():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
