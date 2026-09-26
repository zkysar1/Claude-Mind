"""test_graceful_stop_d7_signoff.py — D7 itself signs the stop off ().

MEASURED (a DEV vessel run, 2026-09-24): the mind ran D7 and D7.05, then ended
its turn without D7.1. D7.1 was the only step that cleared
stop-checkpoint.json, and the vessel sidecar reads that file's absence as the
mind's sign-off: its completion predicate is agent-mode assistant|reader AND
stop-requested, stop-target-mode and stop-checkpoint.json all absent. So the run
idled out its grace and ended on the duration cap instead of on the mind's stop.
The clear now rides D7's own && chain, after the mode flip, in a call the model
demonstrably runs.

Every behavioural lane runs the REAL D7 command, lifted out of SKILL.md, in a
copied-core sandbox and in the vessel's call shape: cwd = the workspace root,
MIND_AGENT exported, MIND_AGENT absent from the environment (the command's own
prefixes carry it), STORAGE_BACKEND=local (guard-955).

Lanes:
  1. a passing D7 leaves the sidecar's completion predicate TRUE, with no
     further tool call
  2. a failing session-mode-set.sh leaves the checkpoint present, and FW-11's
     stop-checkpoint.sh resume-needed still fires
  3. a refused handoff check leaves it present too; its refusal holds
     D7.05/D7.1 for the passing re-run instead of forbidding D7.1, and that
     re-run signs the stop off
  4. static: in the D7 line the clear follows the mode flip and the
     stop-target-mode removal, and the skill no longer says not to run D7.1
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
CORE = SCRIPTS.parent
REPO = CORE.parent
for p in (SCRIPTS, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _bash_helpers import BASH  # noqa: E402
import stop_checkpoint as sc  # noqa: E402
import stop_handoff_check as shc  # noqa: E402

SKILL = REPO / ".claude" / "skills" / "aspirations-graceful-stop" / "SKILL.md"
MODE_FLIP = 'session-mode-set.sh "{target_mode}"'
HANDOFF = "session_number: 7\nnext_focus: finish g-1\nfirst_action:\n  goal_id: g-1\n"
# The sidecar's completion predicate, mirrored: Zak-Code
# src/zakcode/session/framework_stop.py framework_stop_complete().
STOPPED_MODES = ("assistant", "reader")
IN_PROGRESS = ("stop-requested", "stop-target-mode", sc.CHECKPOINT_NAME)


def _d7_lines() -> list:
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("Bash:") and MODE_FLIP in ln]
    assert len(starts) == 1, starts
    end = next(i for i in range(starts[0], len(lines)) if lines[i].startswith("fi && echo"))
    return lines[starts[0]:end + 1]


def _d7_command(agent: str, target_mode: str) -> str:
    """The D7 Bash call verbatim: the `Bash:` line, its heredocs, through `fi && ...`."""
    cmd = "\n".join(_d7_lines())[len("Bash:"):].strip()
    return cmd.replace("<agent>", agent).replace("{target_mode}", target_mode)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mind-workspace")
    ignore = shutil.ignore_patterns("__pycache__", "tests")
    for name in ("scripts", "config"):
        shutil.copytree(CORE / name, root / "core" / name, ignore=ignore, symlinks=False)
    (root / "core" / "logs").mkdir(parents=True, exist_ok=True)
    return root


def _stopping_agent(root: Path, agent: str, tmp: Path, *, handoff: bool) -> Path:
    """The state D7 meets: D1 set IDLE, /stop wrote the target mode, GS-0 the checkpoint."""
    adir = root / "agents" / agent
    sess = adir / "session"
    sess.mkdir(parents=True)
    world, meta = tmp / "world", tmp / "meta"
    world.mkdir()
    meta.mkdir()
    (adir / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n", encoding="utf-8")
    (sess / "agent-state").write_text("IDLE\n", encoding="utf-8")
    (sess / "agent-mode").write_text("autonomous\n", encoding="utf-8")
    (sess / "stop-target-mode").write_text("assistant\n", encoding="utf-8")
    rec = sc.write_checkpoint(sess, "assistant")
    rec["stop_started_at"] = shc._iso(time.time() - 60)
    (sess / sc.CHECKPOINT_NAME).write_text(json.dumps(rec), encoding="utf-8")
    if handoff:
        (sess / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")
    return sess


def _vessel_env(agent: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_") and k != "MIND_AGENT"}
    env.update(STORAGE_BACKEND="local", MIND_AGENT=agent)
    return env


def _bash(root: Path, agent: str, cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", cmd], cwd=root, env=_vessel_env(agent),
                          capture_output=True, text=True, timeout=180)


def _run_d7(root: Path, agent: str, target_mode: str) -> subprocess.CompletedProcess:
    return _bash(root, agent, _d7_command(agent, target_mode))


def _resume_needed(root: Path, agent: str) -> subprocess.CompletedProcess:
    # The Session Start Protocol's FW-11 probe, as start/SKILL.md calls it.
    return _bash(root, agent, f"MIND_AGENT={agent} bash core/scripts/stop-checkpoint.sh resume-needed")


def _mode(sess: Path) -> str:
    return (sess / "agent-mode").read_text(encoding="utf-8").strip()


def _signed_off(sess: Path) -> bool:
    return _mode(sess) in STOPPED_MODES and not any((sess / n).exists() for n in IN_PROGRESS)


def test_passing_d7_signs_the_stop_off_in_the_same_call(workspace, tmp_path):
    sess = _stopping_agent(workspace, "zz-d7-pass", tmp_path, handoff=True)
    assert not _signed_off(sess)
    proc = _run_d7(workspace, "zz-d7-pass", "assistant")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Stop verified" in proc.stdout
    assert _mode(sess) == "assistant"
    assert not (sess / sc.CHECKPOINT_NAME).exists()
    assert _signed_off(sess)


def test_failing_mode_set_leaves_the_checkpoint_for_resume(workspace, tmp_path):
    sess = _stopping_agent(workspace, "zz-d7-modefail", tmp_path, handoff=True)
    proc = _run_d7(workspace, "zz-d7-modefail", "not-a-mode")
    assert proc.returncode != 0
    assert "Invalid mode" in proc.stderr  # the mode flip failed, not the handoff check
    assert "Stop verified" not in proc.stdout
    assert _mode(sess) == "autonomous"
    assert (sess / "stop-target-mode").exists()
    assert (sess / sc.CHECKPOINT_NAME).exists()
    rn = _resume_needed(workspace, "zz-d7-modefail")
    assert rn.returncode == 0, rn.stdout + rn.stderr
    assert json.loads(rn.stdout)["resume_needed"] is True


def test_refused_d7_holds_the_tail_steps_then_the_rerun_signs_off(workspace, tmp_path):
    sess = _stopping_agent(workspace, "zz-d7-refused", tmp_path, handoff=False)
    proc = _run_d7(workspace, "zz-d7-refused", "assistant")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "STOP NOT FINISHED" in proc.stdout
    assert _mode(sess) == "autonomous"
    assert (sess / sc.CHECKPOINT_NAME).exists()
    assert _resume_needed(workspace, "zz-d7-refused").returncode == 0
    # The refusal the model reads must sequence D7.1, never forbid it.
    assert "Do NOT run D7.1" not in proc.stdout
    assert "Re-run the D7 command unchanged." in proc.stdout
    assert "run D7.05 and D7.1" in proc.stdout
    (sess / "handoff.yaml").write_text(HANDOFF, encoding="utf-8")
    again = _run_d7(workspace, "zz-d7-refused", "assistant")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _signed_off(sess)


def test_d7_clears_only_after_the_mode_flip_and_never_forbids_d71():
    line = _d7_lines()[0]
    clear = line.index("stop-checkpoint.sh clear")
    assert line.index(MODE_FLIP) < clear
    assert line.index("rm -f agents/<agent>/session/stop-target-mode") < clear
    assert "do not run d7.1" not in SKILL.read_text(encoding="utf-8").lower()
