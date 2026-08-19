"""test_iteration_commit_foreign_inflight_anchor.py — regression test for
g-115-6107.

The pre-claim mtime filter anchors on team-state
`agent_status.<agent>.in_flight.claimed_at` — an AGENT-keyed row that can name
a DIFFERENT goal than the one being committed (a held second claim, or a stale
row). Measured 2026-08-13 (cc-04): while closing g-115-5216 the row held
g-350-174 (re-claimed at 06:21:02), so both deliverable files — authored
post-claim for the CLOSING goal at 06:11/06:12 — "predated" the foreign anchor
and were silently filtered. The close reported success; the commit carried
only bookkeeping.

Fix under test: iteration-commit.sh reads `in_flight.goal_id` and uses the
row's claimed_at ONLY when the row names the goal being committed. On a
mismatch the filter is left inert (anchor 0 — the documented normal-path RULE)
and a loud ERROR names both ids. Standing bias per g-115-1182: never silently
drop self-authored work.

Production shape (guard-920): the shim team-state-read.sh answers the exact
field queries the script issues, the file's mtime falls BETWEEN the closing
goal's (implicit) claim and the foreign row's claimed_at, and the script is
invoked with --goal-id naming a goal the row does not hold.

Cases:
  1. FOREIGN row (goal_id != --goal-id): file predating the row's claimed_at
     is NOT filtered, and the ERROR marker names both ids.  [the incident]
  2. MATCHING row (goal_id == --goal-id): file predating claimed_at IS
     filtered — the partner-WIP protection is unchanged in the legit case.
  3. FOREIGN row + --include-untracked: no filter, no ERROR noise gate broken
     (override path unchanged).

Pattern: subprocess + tempdir + scripted git init, mirroring
test_iteration_commit_untracked_filter.py (same shim mechanics).
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_foreign_anchor_test"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _run_bash(args, env=None, cwd=None):
    cmd = [GIT_BASH] + [_to_bash_path(a) for a in args]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, env=full_env, cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def _setup_repo(tmpdir: Path) -> Path:
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "agents").mkdir()
    for a in ("alpha", "zeta"):
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _field_aware_shim(shim_dir: Path, inflight_goal: str | None,
                      claimed_at_iso: str | None) -> None:
    """team-state-read.sh shim that dispatches on the --field argument.

    The committer's own in_flight.goal_id / in_flight.claimed_at answer with
    the provided values; every other field (partner rows) answers null so the
    partner-snapshot filter stays inert and the pre-claim filter is isolated.
    """
    goal_line = f'echo \'"{inflight_goal}"\'' if inflight_goal is not None else 'echo null'
    claim_line = f'echo \'"{claimed_at_iso}"\'' if claimed_at_iso is not None else 'echo null'
    body = f"""#!/usr/bin/env bash
field=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --field) field="${{2:-}}"; shift 2 ;;
    *) shift ;;
  esac
done
case "$field" in
  agent_status.alpha.in_flight.goal_id) {goal_line} ;;
  agent_status.alpha.in_flight.claimed_at) {claim_line} ;;
  *) echo null ;;
esac
exit 0
"""
    shim = shim_dir / "team-state-read.sh"
    shim.write_text(body)
    shim.chmod(0o755)


def _shim_iteration_commit(tmpdir: Path, inflight_goal: str | None,
                           claimed_at_iso: str | None) -> Path:
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    _field_aware_shim(shim_dir, inflight_goal, claimed_at_iso)
    return target


ROW_CLAIMED_AT = "2026-08-13T06:21:02"
ROW_EPOCH = int(datetime.datetime.fromisoformat(ROW_CLAIMED_AT).timestamp())


def _seed_deliverable(repo: Path, name: str) -> Path:
    """Untracked neutral-path file authored 5 min BEFORE the row's claimed_at
    — the incident geometry (post-claim for the closing goal, pre-claim for
    the foreign row)."""
    f = repo / "core" / "scripts" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# closing goal deliverable\n")
    mtime = ROW_EPOCH - 300
    os.utime(f, (mtime, mtime))
    return f


def test_foreign_inflight_row_does_not_filter_own_deliverable():
    """in_flight holds g-OTHER-99 while committing g-test-01: the deliverable
    predating the FOREIGN claimed_at must be COMMITTED, with a loud ERROR
    naming both ids. This is the g-115-6107 incident shape; it FAILS against
    the pre-fix script (file filtered, no ERROR)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "g-OTHER-99", ROW_CLAIMED_AT)
        _seed_deliverable(repo, "closing-goal-deliverable.py")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        filter_lines = [L for L in combined.splitlines()
                        if "filtered (cross-agent-uncommitted):" in L]
        assert not any("closing-goal-deliverable.py" in L for L in filter_lines), \
            f"FOREIGN anchor filtered the closing goal's own deliverable. filter_lines={filter_lines!r}"
        assert "closing-goal-deliverable.py" in combined, \
            f"Deliverable missing from staging plan. combined={combined!r}"
        # Loudness: the refusal names both ids at ERROR level.
        assert "g-OTHER-99" in combined and "g-test-01" in combined \
            and "FOREIGN anchor" in combined, \
            f"Foreign-anchor ERROR marker missing. combined={combined!r}"


def test_matching_inflight_row_still_filters_predating_file():
    """in_flight holds the SAME goal being committed: a file predating its
    claimed_at is filtered exactly as before — the partner-WIP protection is
    not weakened in the legitimate case."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "g-test-01", ROW_CLAIMED_AT)
        _seed_deliverable(repo, "predates-own-claim.py")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (cross-agent-uncommitted)" in combined \
            and "predates-own-claim.py" in combined, \
            f"Matching-row filter no longer fires. combined={combined!r}"
        assert "FOREIGN anchor" not in combined, \
            f"Foreign-anchor ERROR fired on a MATCHING row. combined={combined!r}"


def test_foreign_row_with_include_untracked_override():
    """--include-untracked keeps its semantics under a foreign row: filter off
    (anchor block never runs), file staged, no spurious ERROR."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp, "g-OTHER-99", ROW_CLAIMED_AT)
        _seed_deliverable(repo, "override-path.py")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run",
             "--include-untracked"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (cross-agent-uncommitted)" not in combined, \
            f"Filter fired despite --include-untracked. combined={combined!r}"
        assert "override-path.py" in combined, \
            f"File missing from staging plan under override. combined={combined!r}"


if __name__ == "__main__":
    test_foreign_inflight_row_does_not_filter_own_deliverable()
    test_matching_inflight_row_still_filters_predating_file()
    test_foreign_row_with_include_untracked_override()
    print("All 3 tests passed.")
