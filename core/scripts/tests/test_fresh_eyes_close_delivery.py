"""Contract pin: /fresh-eyes-close's delivery probe does what its text says.

g-375-107. The skill's traceability check had no delivery step, so a reviewer
that followed the skill alone approved a closure whose commit sat on a worker
ref and never reached origin/main (g-375-20 occurrence 74, measured 2026-10-01).
The step now lives in SKILL.md as a fenced bash block, and a command written in
prose is an untested claim about an interface (rb-7236).

Design notes:

* The commands are read out of the SKILL.md block itself, not from a copy in
  this file (guard-2224). Edit the block and this test runs the edited lines.
* The fixture is a real bare origin with three clones: a seed that owns main,
  a worker that pushes only to refs/workers/**, and the reviewer's clone.
  Nothing is stubbed below git, so the fetch refspecs, the goal-id grep and the
  probe's ref classification all run for real.
* Each guard in the block has a control that shows the pin can fail. The
  worker commit is invisible to a default clone until the fetch line runs. A
  goal-id grep without the digit guard also matches a longer id. `--all`
  without `--exclude=refs/stash` lists a churn stash that names the goal. A
  carried commit is still no ancestor of main, so only `cherry` shows it landed.
"""
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / ".claude" / "skills" / "fresh-eyes-close" / "SKILL.md"

GOAL = "g-9-10"
LONGER = "g-9-100"  # shares GOAL as a prefix: the digit guard must not match it
SID = "9c5cc235-0000-4000-8000-000000000001"


def _probe_lines():
    """The delivery probe's commands, straight out of the SKILL.md block."""
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(# Delivery probe\..*?)```", text, re.S)
    assert len(blocks) == 1, (
        f"expected one '# Delivery probe.' bash block in {SKILL.name}, found "
        f"{len(blocks)}; re-point this pin at the new block rather than deleting it"
    )
    joined = blocks[0].replace("\\\n", " ")
    return [ln.strip() for ln in joined.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _documented():
    """{fetch, log, reach, cherry} -> the documented command for each step."""
    lines = _probe_lines()
    heads = {"fetch": "git -C <repo> fetch ", "log": "git -C <repo> log ",
             "reach": "py -3 core/scripts/commit-reachability.py ",
             "cherry": "git -C <repo> cherry "}
    assert len(lines) == len(heads), lines
    found = {}
    for name, head in heads.items():
        hits = [ln for ln in lines if ln.startswith(head)]
        assert len(hits) == 1, (name, lines)
        found[name] = hits[0]
    return found


def _render(cmd, repo, sha=""):
    # POSIX paths: bash strips the backslashes of a str(WindowsPath) (guard-581).
    out = (cmd.replace("<repo>", shlex.quote(Path(repo).as_posix()))
              .replace("<goal-id>", GOAL)
              .replace("<sha>", sha))
    if out.startswith("py -3 "):
        out = shlex.quote(Path(sys.executable).as_posix()) + out[len("py -3"):]
    assert not re.search(r"<(repo|goal-id|sha)>", out), out
    return out


def _sh(cmd, check=True):
    # cwd is the framework checkout, so the probe's relative script path resolves.
    p = subprocess.run([BASH, "-c", cmd], cwd=REPO, capture_output=True,
                       text=True, timeout=120)
    if check:
        assert p.returncode == 0, (cmd, p.returncode, p.stderr)
    return p


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout.strip()


def _clone(origin, path):
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True)
    _git(path, "config", "user.email", "fixture@example.invalid")
    _git(path, "config", "user.name", "fixture")
    return path


def _commit(repo, name, body, message):
    (Path(repo) / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def box(tmp_path):
    """A fresh origin per test: some tests move main, and none may see another's."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.email", "fixture@example.invalid")
    _git(seed, "config", "user.name", "fixture")
    _git(seed, "remote", "add", "origin", str(origin))
    shas = {"base": _commit(seed, "app.txt", "one\n", "base")}
    shas["longer"] = _commit(seed, "other.txt", "x\n", f"feat({LONGER}): another goal")
    _git(seed, "push", "-q", "origin", "main")

    # The worker's only push goes to its carrier ref, never to main.
    worker = _clone(origin, tmp_path / "worker")
    shas["worker"] = _commit(worker, "app.txt", "one\ntwo\n", f"fix({GOAL}): the change")
    _git(worker, "push", "-q", "origin", f"HEAD:refs/workers/alpha/{SID}")

    # The reviewer's clone holds a churn stash whose message names the goal.
    reviewer = _clone(origin, tmp_path / "reviewer")
    (reviewer / "other.txt").write_text("churn\n")
    _git(reviewer, "stash", "push", "-q", "-m", f"churn while on {GOAL}")
    shas["stash"] = _git(reviewer, "rev-parse", "refs/stash")
    return {"seed": seed, "worker": worker, "reviewer": reviewer, "shas": shas}


def _reach(box, sha):
    return json.loads(_sh(_render(_documented()["reach"], box["reviewer"], sha)).stdout)


def _goal_commits(box, cmd=None):
    out = _sh(_render(cmd or _documented()["log"], box["reviewer"])).stdout
    return set(out.split())


def test_the_block_holds_the_four_documented_steps():
    found = _documented()
    assert "refs/workers/*" in found["fetch"]
    assert "--exclude=refs/stash --all" in found["log"]
    assert "--target-ref origin/main" in found["reach"]
    assert found["cherry"].endswith("<sha> <sha>^")


def test_the_goal_search_finds_the_worker_commit_only_after_the_fetch_line(box):
    # Control: a default clone fetches no worker refs, so the search is blind to them.
    assert box["shas"]["worker"] not in _goal_commits(box)
    _sh(_render(_documented()["fetch"], box["reviewer"]))
    assert _goal_commits(box) == {box["shas"]["worker"]}


def test_the_digit_guard_keeps_a_longer_goal_id_out(box):
    _sh(_render(_documented()["fetch"], box["reviewer"]))
    log = _documented()["log"]
    unguarded = log.replace("([^0-9]|$)", "")
    assert unguarded != log
    assert box["shas"]["longer"] in _goal_commits(box, unguarded)
    assert box["shas"]["longer"] not in _goal_commits(box, log)


def test_the_stash_exclusion_keeps_a_churn_stash_out(box):
    _sh(_render(_documented()["fetch"], box["reviewer"]))
    log = _documented()["log"]
    unexcluded = log.replace("--exclude=refs/stash ", "")
    assert unexcluded != log
    assert box["shas"]["stash"] in _goal_commits(box, unexcluded)
    assert box["shas"]["stash"] not in _goal_commits(box, log)


def test_a_commit_only_on_a_worker_ref_is_stranded(box):
    _sh(_render(_documented()["fetch"], box["reviewer"]))
    got = _reach(box, box["shas"]["worker"])
    assert got["verdict"] == "STRANDED_WORKER_REF", got
    assert any(f"alpha/{SID}" in ref for ref in got["containing_refs"]["worker"]), got
    assert _reach(box, box["shas"]["base"])["verdict"] == "LANDED"


def test_the_same_commit_on_main_is_landed(box):
    _git(box["worker"], "push", "-q", "origin", "HEAD:main")
    _sh(_render(_documented()["fetch"], box["reviewer"]))
    got = _reach(box, box["shas"]["worker"])
    assert got["verdict"] == "LANDED", got


def test_cherry_shows_a_carried_commit_landed_by_content(box):
    d = _documented()
    _sh(_render(d["fetch"], box["reviewer"]))
    cherry = _render(d["cherry"], box["reviewer"], box["shas"]["worker"])
    # Control: before any carry, the change is on main by neither ancestry nor content.
    assert _sh(cherry).stdout.startswith("+ ")

    # A reducer-style carry: the same hunk re-applied on main under a new sha.
    (box["seed"] / "app.txt").write_text("one\ntwo\n")
    _git(box["seed"], "commit", "-q", "-am", "carry the worker hunk")
    _git(box["seed"], "push", "-q", "origin", "main")
    _sh(_render(d["fetch"], box["reviewer"]))
    assert _reach(box, box["shas"]["worker"])["verdict"] == "STRANDED_WORKER_REF"
    assert _sh(cherry).stdout.startswith(f"- {box['shas']['worker']}")


def test_the_head_test_parts_a_local_commit_on_head_from_one_on_a_side_branch(box):
    text = SKILL.read_text(encoding="utf-8")
    head_test = re.findall(r"`(git merge-base --is-ancestor <sha> HEAD)`", text)
    assert len(head_test) == 1, head_test
    reviewer = box["reviewer"]
    on_head = _commit(reviewer, "local.txt", "a\n", f"fix({GOAL}): local on HEAD")
    _git(reviewer, "checkout", "-q", "-b", "side", "HEAD~1")
    on_side = _commit(reviewer, "side.txt", "b\n", f"fix({GOAL}): local on a side branch")
    _git(reviewer, "checkout", "-q", "main")
    run = f"git -C {shlex.quote(reviewer.as_posix())} " + head_test[0][len("git "):]
    for sha in (on_head, on_side):
        assert _reach(box, sha)["verdict"] == "STRANDED_LOCAL_ONLY"
    assert _sh(run.replace("<sha>", on_head), check=False).returncode == 0
    assert _sh(run.replace("<sha>", on_side), check=False).returncode == 1
