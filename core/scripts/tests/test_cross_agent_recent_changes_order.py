"""test_cross_agent_recent_changes_order.py — the /fresh-eyes-code --since
target set keeps code files inside its 20-file cap (g-115-11171).

cross-agent-recent-changes.sh produces the --since target set, and
/fresh-eyes-code Phase 1 caps it with `sed '/^$/d' | head -20`. The producer
used to emit `sort -u` order, which puts '.claude/' and 'agents/' ahead of
'core/'. The cap then kept agent state and dropped every core/scripts file:
over 151 window paths, 42 of them core/scripts, head -20 kept 0
(msg-20260927-140123-alpha-2971).

Newest-first ALONE does not fix it. Agent-state churn commits are the newest
commits in an active window, so recency fills the cap with agents/ paths.
Measured 2026-09-28 (hostname cc-07) on a 48h window of 525 paths, 178 under
the code roots: newest-first put 19 agents/ paths and 1 code file in the first
20. The producer therefore emits the code roots first, newest-first within
each tier.

The fixture has that production shape. Its code files are committed FIRST
(oldest), and 25 agents/ state paths are committed after them. Two controls
run on the SAME fixture and show that it discriminates:
  - the pre-fix `sort -u | head -20` pipeline keeps 0 of the core/scripts and
    mind_api files, which sort after agents/;
  - recency alone keeps 0 code files of any root.
So a green run can only come from the ranking itself.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
from pathlib import Path

import pytest

from _bash_helpers import BASH

SCRIPT = Path(__file__).resolve().parent.parent / "cross-agent-recent-changes.sh"
CODE_RE = re.compile(r"^(core/scripts|\.claude/skills|mind_api)/")
CAP = 20  # /fresh-eyes-code Phase 1: `sed '/^$/d' | head -20`

BASE = 1_790_000_000  # fixed epoch, so commit order is deterministic
SINCE = datetime.datetime.fromtimestamp(BASE - 600, tz=datetime.timezone.utc).isoformat()

OLD = ["agents/alpha/old-state.jsonl", "core/scripts/old_outside_window.sh"]
# One commit per step, oldest first. The last code commit RE-TOUCHES
# a_tool.py, so its newest touch must decide its position.
CODE_COMMITS = [
    ["core/scripts/a_tool.py"],
    ["core/scripts/z_tool.sh"],
    ["mind_api/src/handler.py"],
    [".claude/skills/demo/SKILL.md"],
    ["core/scripts/a_tool.py", "core/scripts/tests/test_tool.py"],
]
AGENT_COMMITS = [
    [f"agents/alpha/state-{i:02d}.jsonl" for i in range(start, start + 5)]
    for start in range(1, 26, 5)
]
PROSE = "core/config/demo.yaml"  # a non-code, non-agent path in the rest tier

# Expected output: git lists a commit's files in path order; commits newest first.
EXPECTED_CODE = [
    "core/scripts/a_tool.py",
    "core/scripts/tests/test_tool.py",
    ".claude/skills/demo/SKILL.md",
    "mind_api/src/handler.py",
    "core/scripts/z_tool.sh",
]
EXPECTED_REST = [p for c in reversed(AGENT_COMMITS[1:]) for p in c] + AGENT_COMMITS[0] + [PROSE]


def _git(repo: Path, *args: str, ts: int | None = None) -> None:
    env = dict(os.environ)
    if ts is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{ts} +0000"
    subprocess.run(["git", *args], cwd=str(repo), env=env, check=True,
                   capture_output=True, text=True)


def _commit(repo: Path, paths: list[str], ts: int) -> None:
    for rel in paths:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{rel} @ {ts}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"touch {len(paths)} path(s)", ts=ts)


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("recent-changes")
    repo = root / "repo"
    repo.mkdir()
    hooks = root / "no-hooks"
    hooks.mkdir()
    _git(repo, "init", "-q")
    for key, value in (("user.email", "test@example.com"), ("user.name", "order-test"),
                       ("commit.gpgsign", "false"), ("core.hooksPath", str(hooks))):
        _git(repo, "config", key, value)
    _commit(repo, OLD, BASE - 3600)  # before the --since cutoff
    ts = BASE
    for paths in CODE_COMMITS:
        _commit(repo, paths, ts)
        ts += 60
    for i, paths in enumerate(AGENT_COMMITS):
        _commit(repo, paths + ([PROSE] if i == 0 else []), ts)
        ts += 60
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, STORAGE_BACKEND="local")
    return subprocess.run([BASH, SCRIPT.as_posix(), *args], cwd=str(repo), env=env,
                          capture_output=True, text=True, timeout=120)


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


def _shell(repo: Path, pipeline: str) -> list[str]:
    env = dict(os.environ, LC_ALL="C")
    proc = subprocess.run([BASH, "-c", pipeline], cwd=str(repo), env=env,
                          capture_output=True, text=True, timeout=60, check=True)
    return _lines(proc.stdout)


@pytest.fixture(scope="module")
def ranked(repo: Path) -> list[str]:
    proc = _run(repo, "--since", SINCE)
    assert proc.returncode == 0, proc.stderr
    return _lines(proc.stdout)


def test_code_files_survive_the_cap(ranked):
    capped = ranked[:CAP]
    assert [p for p in capped if CODE_RE.match(p)] == EXPECTED_CODE


def test_order_is_code_tier_then_rest_newest_first_each(ranked):
    assert ranked == EXPECTED_CODE + EXPECTED_REST


def test_reorder_loses_and_duplicates_nothing(ranked):
    assert len(ranked) == len(set(ranked)) == 31
    assert not set(OLD) & set(ranked)


# The pre-fix producer's Step 1 pipeline, verbatim, capped as Phase 1 caps it.
PRE_FIX = (
    "git log --name-only --pretty=format:'%x01%ct' | awk -v c={epoch} "
    "'/^\\x01/ {{ keep = (substr($0,2) + 0) >= c; next }} keep && length($0) {{ print }}' "
    "| sed '/^$/d' | sort -u"
)


def test_control_pre_fix_alphabetical_cap_drops_core_scripts(repo, ranked):
    full = _shell(repo, PRE_FIX.format(epoch=BASE - 600))
    assert sorted(full) == sorted(ranked)  # same population as the fixed output
    late_sorting = [p for p in EXPECTED_CODE if p.startswith(("core/scripts/", "mind_api/"))]
    assert len(late_sorting) == 4
    assert not set(late_sorting) & set(full[:CAP])


def test_control_recency_alone_drops_every_code_file(repo, ranked):
    newest_first = _shell(repo, PRE_FIX.format(epoch=BASE - 600).replace("sort -u", "awk '!seen[$0]++'"))
    assert sorted(newest_first) == sorted(ranked)
    assert not [p for p in newest_first[:CAP] if CODE_RE.match(p)]


def test_unparseable_since_falls_back_to_full_history_ranked(repo):
    proc = _run(repo, "--since", "not-a-timestamp")
    assert proc.returncode == 0
    assert "scanning full history" in proc.stderr
    out = _lines(proc.stdout)
    assert len(out) == len(set(out)) == 33
    assert out[:6] == EXPECTED_CODE + ["core/scripts/old_outside_window.sh"]
    assert out[-1] == "agents/alpha/old-state.jsonl"
