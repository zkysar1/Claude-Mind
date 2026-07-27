"""Regression tests for core/scripts/check-no-bare-agent-prefix.sh.

Covers BOTH prefix classes the gate defends:
  class 1  BARE_AGENT_PREFIX_REGRESSION — `<agent>/X/` with no `agents/`
  class 2  BARE_WORLD_PREFIX_REGRESSION — `bash world/X` in an executable line
           (added g-115-3130)

Each case builds a throwaway git repo and runs the gate in its default
precommit mode against staged content, which is the code path the
`core/githooks/pre-commit` Gate 7 actually invokes.
"""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check-no-bare-agent-prefix.sh"
TARGET = ".claude/rules/fixture.md"


def _repo(tmp_path, body: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    f = tmp_path / TARGET
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", TARGET], cwd=tmp_path, check=True)
    return tmp_path


def _run(repo: Path):
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True
    )


# --- class 2: forms that MUST be refused -----------------------------------

@pytest.mark.parametrize("line", [
    "Bash: bash world/scripts/x.sh --arg",       # `bash world/` form
    "Bash: world/scripts/x.sh --arg",            # no `bash` word (most common)
    "Bash: meta/scripts/x.sh",                   # meta/ is external too
    "    out=$(bash world/scripts/x.sh 2>/dev/null)",  # inside a substitution
    "bash world/scripts/x.sh --arg",             # column 1 — see note below
])
def test_class2_bare_world_prefix_is_refused(tmp_path, line):
    """A bare world//meta/ prefix in an executable line fails the gate.

    The column-1 case is a specific regression guard: the detector counts
    backticks preceding the match to skip `code spans`, and an early
    implementation used split() — which returns 0 on an empty prefix, making
    the count -1 (odd) and silently skipping EVERY match starting at column 1,
    i.e. the single most common form. gsub() returns 0 there instead.
    """
    r = _run(_repo(tmp_path, line + "\n"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BARE_WORLD_PREFIX_REGRESSION" in r.stderr


# --- class 2: forms that MUST be allowed -----------------------------------

@pytest.mark.parametrize("line", [
    # prose / documented anti-pattern inside a code span
    "A mention of `bash world/scripts/x.sh` in prose.",
    "# `bash world/...` fails with No such file or directory",
    # predicate.py preconditions: the bare form is REQUIRED there — predicate.py
    # rewrites it to an absolute path itself, and its ALLOWED_COMMAND_PREFIXES
    # would REJECT the $WORLD_PATH-resolved form.
    '    command: "bash world/scripts/probe-x.sh"',
    # the canonical resolved form (guard-666)
    'Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/x.sh" --arg',
])
def test_class2_legitimate_forms_are_allowed(tmp_path, line):
    r = _run(_repo(tmp_path, line + "\n"))
    assert r.returncode == 0, r.stderr


# --- class 1 must keep working (no regression from the class-2 addition) ----

def test_class1_bare_agent_prefix_still_refused(tmp_path):
    r = _run(_repo(tmp_path, "Write to <agent>/session/foo\n"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BARE_AGENT_PREFIX_REGRESSION" in r.stderr


def test_class1_correct_agents_prefix_allowed(tmp_path):
    r = _run(_repo(tmp_path, "Write to agents/<agent>/session/foo\n"))
    assert r.returncode == 0, r.stderr


# --- the two classes report independently ----------------------------------

def test_each_class_reports_its_own_diagnostic(tmp_path):
    """A class-2 hit must NOT emit the class-1 diagnostic, and vice versa.

    Both classes previously shared one exit branch and one message; a
    world/-class hit that printed the agent-dir remedy would send the reader
    to the wrong fix.
    """
    body = "Bash: world/scripts/x.sh\nWrite to <agent>/session/foo\n"
    r = _run(_repo(tmp_path, body))
    assert r.returncode == 1
    assert "BARE_WORLD_PREFIX_REGRESSION" in r.stderr
    assert "BARE_AGENT_PREFIX_REGRESSION" in r.stderr
    assert "guard-666" in r.stderr          # class-2 remedy
    assert "Phase 2.5.D" in r.stderr        # class-1 remedy


def test_world_class_alone_still_exits_nonzero(tmp_path):
    """Guards the exit branch: the `exit 1` must key off found_any, not the
    agent class alone, or a world-only hit would report and then exit 0."""
    r = _run(_repo(tmp_path, "Bash: world/scripts/x.sh\n"))
    assert r.returncode == 1
    assert "BARE_AGENT_PREFIX_REGRESSION" not in r.stderr
