#!/usr/bin/env python3
"""test_aspiration_trajectory_code_credit.py - code-credit pins ().

scripts_conventions_authored used to credit a goal only when its id sat in the
first 4000 chars of a TOP-LEVEL script or convention. A Fix that edited an
existing file (its id lands deep in the body, or nowhere) or only its tests
scored 0, while a header that merely CITED a goal credited it. The framework
lane now credits the files a goal's commits CHANGED, read from git history.

The pins mirror the goal's three outcomes:
  1. an edit to an existing script, id past char 4000  -> credited
  2. a change confined to core/scripts/tests/           -> credited
  3. a file that only MENTIONS a goal                   -> not credited,
     beside the positive control shaped like g-306-510 (a new script whose
     header names the goal that created it).

The subject/body forms in test_commit_author_goal_ids come from a census of
the live history (the 4,590 commits touching these lanes, 2026-09-27).
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# aspiration-trajectory.py is hyphenated -> load by path
_spec_at = importlib.util.spec_from_file_location(
    "aspiration_trajectory", SCRIPT_DIR / "aspiration-trajectory.py")
at = importlib.util.module_from_spec(_spec_at)
_spec_at.loader.exec_module(at)

@pytest.fixture(autouse=True)
def _own_repo_only(monkeypatch):
    # Run from inside a git hook, these would point every git call below -- the
    # fixture's commits included -- at the enclosing repository.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(var, raising=False)


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false",
         "-c", f"core.hooksPath={repo.parent / 'no-hooks'}", *args],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"git {args[0]}: {proc.stderr}"


def _commit(repo, message, files):
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, "chore: init", {"README.md": "fixture\n"})
    return root


@pytest.mark.parametrize("subject,body,expected", [
    ("fix(g-306-519): credit authorship", "", {"g-306-519"}),
    ("fix(g-306-510,g-306-512): two goals", "", {"g-306-510", "g-306-512"}),
    ("g-115-9750 outcome 3: demonstrate the chain", "", {"g-115-9750"}),
    ("B1b (g-353-82): admit candidate", "", {"g-353-82"}),
    ("feat(verify): run the checks in one call (g-375-48)", "", {"g-375-48"}),
    ("feat(tree): proactive distill trigger [g-115-1570]", "", {"g-115-1570"}),
    ("fix(stop): route a pending stop (g-373-16 R3)", "", {"g-373-16"}),
    ("revert(g-369-150): remove my duplicate drain", "", {"g-369-150"}),
    # a split child keeps its one-letter suffix; a longer tail annotates the parent
    ("feat(g-376-10-a): the first split child", "", {"g-376-10-a"}),
    ("fix(g-115-1741-followup): register the handler", "", {"g-115-1741"}),
    ("fix(scan): y", "g-376-51-b. Adds the child's half.\n", {"g-376-51-b"}),
    # citations: beside a head id, mid-subject, and git's own revert
    ("fix(g-353-136): cite the seams (Q4 provenance, g-357-44)", "", {"g-353-136"}),
    ("fix(tests): the g-115-9588 regression guard was vacuous", "", set()),
    ("chore: carry a concurrent agent's g-115-3777 edits", "", set()),
    ('Revert "fix(g-115-6470): retry only transient failures"', "", set()),
    # body tags, read only when the subject names no goal
    ("feat(scan): detect clusters", "g-115-3289. Adds the scanner.\n", {"g-115-3289"}),
    ("fix(reads): route invalidate", "Why it broke.\n\ng-115-3764\n", {"g-115-3764"}),
    ("fix(gate): bound the run", "Detail line.\n\nGoal: g-306-401\n", {"g-306-401"}),
    ("fix(gate): cite it", "Tests pass.\n\ng-115-9560.\n", {"g-115-9560"}),
    # a wrapped sentence leaving ids alone on a line names owners, not authors
    ("fix(gate): y", "Reds are pre-existing, owned by\n  g-115-8170 / g-115-8140.\n", set()),
    ("fix(gate): y", "Found by g-115-100 while reviewing.\n", set()),
    ("fix(gate): y", "Detail.\n\nRefs: g-115-8188, guard-5413\n", set()),
    ("fix(g-115-1): z", "g-115-2. A body lead the subject outranks.\n", {"g-115-1"}),
])
def test_commit_author_goal_ids(subject, body, expected):
    assert at.commit_author_goal_ids(subject, body) == expected


def test_edit_to_existing_script_with_id_past_char_4000_is_credited(repo):
    header = '"""tool.py (g-900-01)."""\n' + "# filler line of an existing file\n" * 150
    _commit(repo, "feat(g-900-01): add the tool", {"core/scripts/tool.py": header})
    edited = header + "# g-900-02: harden the parser\n"
    _commit(repo, "fix(g-900-02): harden the tool", {"core/scripts/tool.py": edited})
    # the fixture reproduces the defect shape the header scan could not see
    assert "g-900-02" not in edited[:4000] and "g-900-02" in edited

    credit = at.build_framework_code_attribution(repo)
    assert credit["g-900-02"] == 1
    assert credit["g-900-01"] == 1


def test_change_confined_to_tests_is_credited(repo):
    _commit(repo, "test(g-900-03): pin the tool",
            {"core/scripts/tests/test_tool.py": "def test_tool():\n    pass\n"})
    assert at.build_framework_code_attribution(repo) == {"g-900-03": 1}


def test_positive_control_new_script_named_in_its_header(repo):
    # 's shape: the goal created the files and their header names it
    _commit(repo, "feat(g-900-04): add the sweep", {
        "core/scripts/sweep.py": '"""sweep.py (g-900-04)."""\n',
        "core/scripts/sweep.sh": "#!/usr/bin/env bash\n# sweep.sh (g-900-04)\n",
    })
    assert at.build_framework_code_attribution(repo) == {"g-900-04": 2}


def test_negative_control_file_that_only_mentions_a_goal(repo):
    text = '"""other.py -- see g-900-06 for the background."""\n'
    _commit(repo, "fix(g-900-05): unrelated fix", {"core/scripts/other.py": text})
    # the header scan WOULD have credited : the mention is in its window
    assert "g-900-06" in text[:4000]

    credit = at.build_framework_code_attribution(repo)
    assert credit == {"g-900-05": 1}
    assert "g-900-06" not in credit


def test_distinct_code_files_counted_once(repo):
    _commit(repo, "feat(g-900-07): add a", {
        "core/scripts/a.py": "A = 1\n",
        "core/scripts/fixture.json": "{}\n",
        "core/config/other.yaml": "k: v\n",
        "core/config/conventions/a.md": "# a\n",
    })
    _commit(repo, "fix(g-900-07): a again", {"core/scripts/a.py": "A = 2\n"})
    # a.py once across both commits, a.md once; json/yaml are not code lanes
    assert at.build_framework_code_attribution(repo) == {"g-900-07": 2}


def test_mode_only_change_is_not_credited(repo):
    _commit(repo, "feat(g-900-18): add the runner",
            {"core/scripts/run.sh": "#!/usr/bin/env bash\n"})
    _git(repo, "update-index", "--chmod=+x", "core/scripts/run.sh")
    _git(repo, "commit", "-q", "-m", "chore(g-900-19): normalize the exec bit")
    assert at.build_framework_code_attribution(repo) == {"g-900-18": 1}


def test_body_tags_parse_through_real_git_output(repo):
    _commit(repo, "feat(scan): detect clusters\n\ng-900-16. Adds the scanner.",
            {"core/scripts/scan.py": "S = 1\n"})
    _commit(repo, "fix(reads): route invalidate\n\nWhy it broke.\n\ng-900-17\n\n"
                  "Co-Authored-By: t <t@example.invalid>",
            {"core/scripts/reads.py": "R = 1\n"})
    assert at.build_framework_code_attribution(repo) == {"g-900-16": 1, "g-900-17": 1}


def test_merge_commit_credits_nobody(repo):
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "feat(g-900-10): on a branch", {"core/scripts/side.py": "X = 1\n"})
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "--no-ff", "-q", "-m", "g-900-11: merge the branch", "side")
    assert at.build_framework_code_attribution(repo) == {"g-900-10": 1}


def test_unreadable_history_returns_none_and_names_the_reason(
        repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    assert at.build_framework_code_attribution(repo) is None
    assert "git log rc=128" in capsys.readouterr().err
    monkeypatch.delenv("GIT_DIR")

    def no_git(*_a, **_k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert at.build_framework_code_attribution(repo) is None
    assert "git log failed" in capsys.readouterr().err


def _lanes(tmp_path, monkeypatch):
    """Point the map's framework and world roots at a scratch tree."""
    project, world = tmp_path / "project", tmp_path / "world"
    (project / "core" / "scripts").mkdir(parents=True)
    (project / "core" / "config" / "conventions").mkdir(parents=True)
    (world / "scripts").mkdir(parents=True)
    monkeypatch.setattr(at, "PROJECT_ROOT", project)
    monkeypatch.setattr(at, "CONFIG_DIR", project / "core" / "config")
    monkeypatch.setattr(at, "WORLD_DIR", world)
    (project / "core" / "scripts" / "y.py").write_text('"""y.py (g-900-15)."""\n')
    (world / "scripts" / "w.sh").write_text("# w.sh (g-900-14)\n")


def test_map_credits_framework_from_history_and_world_by_header(tmp_path, monkeypatch, capsys):
    _lanes(tmp_path, monkeypatch)
    monkeypatch.setattr(at, "build_framework_code_attribution", lambda: {"g-900-13": 2})
    # y.py's header mention is NOT scanned while history is readable
    assert at.build_script_convention_attribution_map() == {"g-900-13": 2, "g-900-14": 1}
    assert capsys.readouterr().err == ""


def test_map_falls_back_to_header_scan_and_says_so(tmp_path, monkeypatch, capsys):
    _lanes(tmp_path, monkeypatch)
    monkeypatch.setattr(at, "build_framework_code_attribution", lambda: None)
    assert at.build_script_convention_attribution_map() == {"g-900-15": 1, "g-900-14": 1}
    assert "git history unreadable" in capsys.readouterr().err
