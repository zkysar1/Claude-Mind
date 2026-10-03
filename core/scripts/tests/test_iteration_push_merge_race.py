"""test_iteration_push_merge_race.py — the integrate never silently loses a write
made while it merges, logs git's own words on failure, and names the cap on its
conflicted-paths list (g-115-11674, with g-115-11524 and g-115-11570 merged in).

Hermetic: a bare origin plus two clones under tmp_path, file:// only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _bash_helpers import BASH  # noqa: E402
from test_iteration_push import (  # noqa: E402
    PUSH_SH, _clone_pair, _commit_file, _default_flags, _must, _tip,
)

DRIVER_SLEEP_S = 4
WRITE_AT_S = 2


def _push_cmd(repo: Path, *flags: str) -> list:
    return [BASH, str(PUSH_SH), "--repo", str(repo), *flags]


def _env(log_file: Path) -> dict:
    env = dict(os.environ)
    env["ITERATION_PUSH_LOG_FILE"] = str(log_file)
    return env


def test_write_during_a_slow_merge_driver_is_never_silently_lost(tmp_path):
    """The  A/B shape, run through iteration-push's merge path.

    One file has a merge driver that sleeps; a DIFFERENT file the merge also
    changes is appended to while the driver runs. A plain `git merge` checks
    for local changes only when it starts, so its checkout then overwrote the
    append with rc 0 (measured on git 2.43.0 and 2.45.1). Allowed outcomes:
    the append survives (the integrate refused or re-merged); never a merge
    that landed over it.
    """
    origin, a, b = _clone_pair(tmp_path)
    marker = tmp_path / "driver-was-running"
    # The driver is configured on A only: it is A's integrate that is timed.
    (a / ".git" / "info" / "attributes").write_text("slow.txt merge=slow\n")
    _must(a, "config", "merge.slow.driver",
          f"sleep {DRIVER_SLEEP_S}; touch '{marker}'; cp %B %A")
    _commit_file(a, "slow.txt", "s1\n", "base slow")
    _commit_file(a, "a.txt", "a1\n", "base a")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")

    _commit_file(b, "slow.txt", "s1\ns2-b\n", "B: slow")
    _commit_file(b, "a.txt", "a1\na2-b\n", "B: a")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "slow.txt", "s1\ns2-a\n", "A: slow")  # forces a true merge
    a_tip_before = _tip(a)

    log_file = tmp_path / "push.log"
    proc = subprocess.Popen(_push_cmd(a, *_default_flags("--no-push")),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=_env(log_file))
    time.sleep(WRITE_AT_S)
    with open(a / "a.txt", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("CONCURRENT-WRITE\n")
    out, err = proc.communicate(timeout=120)

    # Positive control: the append really landed while a driver was running.
    assert marker.exists(), f"the slow driver never ran:\n{err}"
    content = (a / "a.txt").read_text(encoding="utf-8")
    assert "CONCURRENT-WRITE" in content, (
        f"the write made during the merge was lost:\n{content}\n{err}")
    if _tip(a) != a_tip_before:
        # Only a merge that re-checked the tree may land; it must carry B's work.
        assert "a2-b" in _must(a, "show", "HEAD:a.txt")
    else:
        assert "would be overwritten" in err, err


def test_a_failed_integrate_writes_gits_own_text_to_the_push_log(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    _commit_file(b, "base.txt", "B version\n", "B: rewrite base")
    _must(b, "push", "-q", "origin", "main")
    _commit_file(a, "base.txt", "A version\n", "A: rewrite base")

    log_file = tmp_path / "push.log"
    r = subprocess.run(_push_cmd(a, *_default_flags("--strict")),
                       capture_output=True, text=True, timeout=120,
                       env=_env(log_file))
    assert r.returncode == 1 and "MERGE CONFLICT" in r.stderr, r.stderr
    logged = log_file.read_text(encoding="utf-8")
    git_lines = [ln for ln in logged.splitlines() if "git: | " in ln]
    assert git_lines, logged
    assert any("CONFLICT" in ln and "base.txt" in ln for ln in git_lines), git_lines


def test_conflicted_paths_line_says_how_many_it_did_not_name(tmp_path):
    origin, a, b = _clone_pair(tmp_path)
    names = [f"f{i:02d}.txt" for i in range(14)]
    for n in names:
        _commit_file(a, n, "base\n", f"base {n}")
    _must(a, "push", "-q", "origin", "main")
    _must(b, "pull", "-q", "origin", "main")
    for n in names:
        _commit_file(b, n, "B\n", f"B {n}")
    _must(b, "push", "-q", "origin", "main")
    for n in names:
        _commit_file(a, n, "A\n", f"A {n}")

    r = subprocess.run(_push_cmd(a, *_default_flags("--strict")),
                       capture_output=True, text=True, timeout=120,
                       env=_env(tmp_path / "push.log"))
    line = [ln for ln in r.stderr.splitlines() if "conflicted paths (" in ln]
    assert line, r.stderr
    assert "conflicted paths (14)" in line[0], line[0]
    assert "+2 more not named" in line[0], line[0]
    assert sum(n in line[0] for n in names) == 12, line[0]
