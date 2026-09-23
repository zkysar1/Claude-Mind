"""A worker Body's post-compaction banner, end to end ().

What it pins. After a compaction a worker Body is told who it is (its binding,
read from its own session files), the goal it holds, and to continue its own
loop. It is NOT told the reducer's things: re-arm the reducer's deadman
sentinel, re-enter /aspirations, or read the agent-wide reasoning snapshot and
execution diary, which belong to the reducer or to another session on the box.

Measured 2026-09-23 on zc-01, worker Body 57c55134. Seven of its compactions got
the reducer's banner. Its first call after six of them was the sentinel re-arm,
and after a stale-anchor one it called Skill(aspirations). With no binding in
any of them, it drifted onto other agents twice right after compactions:
bravo's self.md for its light prime, charlie's session dir for its evidence.
Four more compactions there, and all eight on zc-02, got nothing at all, which
test_postcompact_restore_body_guard.py pins at the hook.

Production shape (guard-920). The hook gets a JSON object on stdin and no
MIND_* env, and on a worker box running-session-id does not exist. The REAL
hook and the REAL restore script run here, copied into a tmp PROJECT_ROOT, so
the Body is detected by the same body_state_path rail production uses. Nothing
re-implements the predicate.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

AGENT = "bannerworker"
BODY_SID = "sid-body-banner-0001"
RUNNER_SID = "sid-runner-banner-0002"
GOAL = "g-375-04"

# What the reducer's banner shows and a worker's must not. The reducer's goal
# id doubles as the tell for the agent-wide snapshot and diary.
REDUCER_GOAL = "g-999-99"

_COPY = [
    "postcompact-restore.sh",
    "postcompact-restore.py",
    "_paths.sh",
    "_platform.sh",
    "_paths.py",
    "_path_helpers.py",
    "_resolve_agent_from_sid.py",
    "_session_binding.py",
    "_agents.py",
]

# Same resolver the tests invoke; see test_postcompact_restore_body_guard.py for
# why a shutil.which() skip would silently skip on Windows.
pytestmark = pytest.mark.skipif(
    not (os.path.isfile(BASH) or shutil.which(BASH)),
    reason="needs a resolvable bash (checked via the same _bash_helpers.BASH the tests invoke)",
)


@pytest.fixture
def repo(tmp_path):
    """A tmp PROJECT_ROOT: the real hook and restore script, one agent, and the
    reducer's agent-wide state present on purpose, so its absence from a
    worker's banner means something."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in _COPY:
        shutil.copy2(CORE_SCRIPTS / name, scripts / name)

    agent_dir = tmp_path / _paths.AGENTS_PARENT_DIR / AGENT
    session = agent_dir / "session"
    session.mkdir(parents=True)
    world = tmp_path / "w"
    meta = tmp_path / "m"
    world.mkdir()
    meta.mkdir()
    # resolve_binding refuses an agent dir with no local-paths.conf.
    (agent_dir / "local-paths.conf").write_text(
        "WORLD_PATH=%s\nMETA_PATH=%s\n" % (world.as_posix(), meta.as_posix()),
        encoding="utf-8")
    _queue(tmp_path, "in-progress")

    (session / "reasoning-snapshot.yaml").write_text(
        "current_reasoning:\n"
        f"  goal: {REDUCER_GOAL} the reducer's goal\n"
        "  next_step: the reducer's next step\n",
        encoding="utf-8")
    (session / "execution-diary.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-23T05:00:00", "goal_id": REDUCER_GOAL,
        "entry_type": "note", "content": "another session's step"}) + "\n",
        encoding="utf-8")
    return tmp_path


def _queue(root: Path, status: str) -> None:
    """The world queue the anchor's live-status check reads."""
    (root / "w" / "aspirations.jsonl").write_text(json.dumps({
        "id": "asp-375", "title": "t", "status": "active",
        "goals": [{"id": GOAL, "title": "g", "status": status}]}) + "\n",
        encoding="utf-8")


def _session_dir(root: Path, sid: str) -> Path:
    return root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / sid


def _bind(root: Path, sid: str) -> Path:
    """The binding /start writes; `session_id` is required by resolve_binding."""
    d = _session_dir(root, sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "binding.yaml").write_text(
        "session_id: %s\nagent: %s\nmode: autonomous\n"
        "started_at: '2026-09-23T05:00:00'\nstarted_by: zakcode\n" % (sid, AGENT),
        encoding="utf-8")
    return d


def _worker(root: Path) -> Path:
    """A worker Body: forked working memory, its manifest, and the anchor its
    claim wrote. No running-session-id: a worker box never writes one."""
    d = _bind(root, BODY_SID)
    (d / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    (d / "body-manifest.yaml").write_text(
        "unitKey: %s\nmindKey: %s\nenv_id: local\nrole: worker\n"
        "reducer_sid: remote\nbody_state: active\n" % (BODY_SID, AGENT),
        encoding="utf-8")
    (d / "iteration-checkpoint.json").write_text(json.dumps({
        "goal_id": GOAL, "aspiration_id": "asp-375", "source": "world",
        "phase": "selected", "selected_at": "2026-09-23T11:35:00"}),
        encoding="utf-8")
    return d


def _run(root: Path, sid: str):
    """Invoke the hook as the harness does: JSON on stdin, no MIND_* env."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    return subprocess.run(
        # BASH, not a bare "bash" (guard-580); .as_posix() (guard-581).
        [BASH, (root / "core" / "scripts" / "postcompact-restore.sh").as_posix()],
        input=json.dumps({"session_id": sid, "source": "compact"}),
        capture_output=True, text=True, env=env, timeout=60)


def _worker_banner(root: Path) -> str:
    _worker(root)
    r = _run(root, BODY_SID)
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "post-compaction, worker Body" in r.stdout, (r.stdout, r.stderr)
    return r.stdout


def test_a_worker_is_told_who_it_is(repo):
    """The binding, from the Body's own files, on a box with no runner file and
    no compact-checkpoint.yaml (which a timed-out PreCompact hook leaves stale
    or missing, g-375-03)."""
    out = _worker_banner(repo)
    body = _session_dir(repo, BODY_SID)
    agent_dir = repo / _paths.AGENTS_PARENT_DIR / AGENT
    assert f"agent:          {AGENT}" in out
    assert f"session:        {BODY_SID}" in out
    assert "role:           worker (body_state active, env_id local, reducer_sid remote)" in out
    assert f"{body.as_posix()}/" in out
    assert (body / "working-memory.yaml").as_posix() in out
    assert (agent_dir / "self.md").as_posix() in out
    assert f"You are agent '{AGENT}'" in out


def test_a_worker_is_told_the_goal_it_holds(repo):
    out = _worker_banner(repo)
    assert f"goal_id:       {GOAL}" in out
    assert "phase:         selected" in out
    assert f"CRITICAL: Your in-flight goal is {GOAL}" in out


def test_a_worker_continues_its_own_loop(repo):
    """Not the reducer's re-entry, and not the reducer's deadman sentinel,
    which worker-loop forbids a worker to arm."""
    out = _worker_banner(repo)
    assert "Skill(worker-loop)" in out
    assert "Re-enter /aspirations loop" not in out
    assert "MANDATORY FIRST CALL" not in out
    assert "<<autonomous-loop-dynamic>>" not in out
    assert "/aspirations precheck" not in out
    assert "Phase -0.5" not in out


def test_a_worker_is_not_shown_other_sessions_state(repo):
    """The agent-wide snapshot and diary are the reducer's or another
    session's. test_the_runner_still_gets_the_full_banner is the positive
    control: the same fixture shows them to the runner."""
    out = _worker_banner(repo)
    assert REDUCER_GOAL not in out
    assert "REASONING SNAPSHOT" not in out
    assert "EXECUTION DIARY" not in out


def test_a_stale_anchor_sends_the_worker_to_its_own_select(repo):
    """A closed goal is not resumed, and fresh work comes from worker-loop's
    SELECT, not the reducer's /aspirations precheck + select."""
    _queue(repo, "completed")
    out = _worker_banner(repo)
    assert "STALE ANCHOR" in out
    assert "re-run worker-loop's Phase 1 SELECT to pick fresh work" in out
    assert "/aspirations precheck" not in out


def test_the_runner_still_gets_the_full_banner(repo):
    """Positive control, same fixture: the runner (its SID is running-session-id,
    and it forks no working memory) still gets the reducer's banner, agent-wide
    state included."""
    agent_dir = repo / _paths.AGENTS_PARENT_DIR / AGENT
    (agent_dir / "session" / "running-session-id").write_text(
        RUNNER_SID + "\n", encoding="utf-8")
    (agent_dir / "session" / "compact-checkpoint.yaml").write_text(
        "session_id: %s\nactive_context: {}\n" % RUNNER_SID, encoding="utf-8")
    _bind(repo, RUNNER_SID)
    r = _run(repo, RUNNER_SID)
    assert r.returncode == 0, (r.returncode, r.stderr)
    out = r.stdout
    assert "=== CONTEXT RESTORED (post-compaction) ===" in out, (out, r.stderr)
    assert "MANDATORY FIRST CALL" in out
    assert "Re-enter /aspirations loop" in out
    assert "REASONING SNAPSHOT" in out and REDUCER_GOAL in out
    assert "worker Body" not in out
