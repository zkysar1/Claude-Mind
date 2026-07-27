"""Regression tests for core/scripts/check-no-bare-bash.py (goal ).

The gate refuses a bare-``"bash"`` argv[0] in Python subprocess calls, which on
win32 resolves to the System32 WSL launcher and can hang forever (guard-580).

What is pinned here, in the order g-115-3171's VERIFICATION names it:
  1. all THREE syntactic forms are flagged — including form (b), the
     concatenation shape that the first g-115-3085 grep sweep MISSED;
  2. prose mentions are NOT flagged — both the ``#``-comment and the
     triple-quoted-docstring shapes, the latter verified against the real
     ``dependent-unblock.py`` the goal names as the canonical prose mention;
  3. the override marker works, so a genuinely POSIX-only path has an escape;
  4. the sanctioned fix (``[BASH, ...]``) is NOT flagged — without this, a
     gate that flagged the fix too would be unsatisfiable;
  5. the gate has DISCRIMINATING POWER (guard-1475): a test that claims to pin
     a rule must FAIL when the rule is removed. ``test_detection_is_load_bearing``
     removes each detection rule from a copy of the module and asserts the
     corresponding fixture stops being flagged — proving these tests measure the
     gate rather than merely co-existing with it.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from _bash_helpers import BASH

GATE_PY = Path(__file__).resolve().parents[1] / "check-no-bare-bash.py"
GATE_SH = Path(__file__).resolve().parents[1] / "check-no-bare-bash.sh"
REPO = Path(__file__).resolve().parents[3]


def _load(path: Path = GATE_PY):
    """Import the hyphenated gate module under a unique name."""
    spec = importlib.util.spec_from_file_location(f"_gate_{path.stem}_{id(path)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load()


# --- 1. the three forms that MUST be refused --------------------------------

FORM_A = 'import subprocess\nsubprocess.run(["bash", "x.sh"], check=True)\n'
FORM_B = 'cmd = ["sh"]\ncmd = ["bash"] + cmd\n'
FORM_C = 'import subprocess\nsubprocess.run("bash x.sh --flag", shell=True)\n'


@pytest.mark.parametrize(
    "label,src",
    [("a-call-site-literal", FORM_A), ("b-concatenation", FORM_B), ("c-shell-string", FORM_C)],
)
def test_all_three_forms_are_flagged(label, src):
    hits = GATE.scan_source(src, f"core/scripts/{label}.py")
    assert hits, f"form {label} MUST be flagged — got no hits"


def test_form_b_is_the_one_the_first_sweep_missed():
    """Explicit guard on the concatenation form.

    g-115-3171 records that the first sweep missed ``cmd = ["bash"] + cmd`` and
    only a second, broader grep found it in monitor-tick.py. Detection here is
    'any list literal whose first element is "bash"', which catches the shape
    wherever it appears — no dataflow analysis and no call-site requirement.
    """
    hits = GATE.scan_source(FORM_B, "core/scripts/x.py")
    assert len(hits) == 1
    assert hits[0][1] == "list-argv"


def test_fstring_shell_command_is_flagged():
    src = 'import subprocess\nsubprocess.run(f"bash {s}", shell=True)\n'
    assert GATE.scan_source(src, "core/scripts/x.py")


def test_bash_exe_spelling_is_flagged():
    assert GATE.scan_source('subprocess.run(["bash.exe", "x"])\n', "core/scripts/x.py")


# --- 2. prose mentions that MUST NOT be flagged -----------------------------

def test_hash_comment_prose_is_not_flagged():
    src = '# we used to call subprocess.run(["bash", ...]) here\nx = 1\n'
    assert GATE.scan_source(src, "core/scripts/x.py") == []


def test_docstring_prose_is_not_flagged():
    """The shape that defeats a comment-stripping grep.

    This is why the gate is AST-based: the mention spans a triple-quoted
    docstring, so `grep -v '^\\s*#'` would not strip it.
    """
    src = 'def f():\n    """When bash receives subprocess.run(["bash", ...]) it fails."""\n    return 1\n'
    assert GATE.scan_source(src, "core/scripts/x.py") == []


def test_real_dependent_unblock_prose_mention_is_not_flagged():
    """The canonical prose mention  names by file.

    dependent-unblock.py's _update docstring quotes the bad pattern while
    explaining the .as_posix() fix. Flagging it would be the exact
    false positive the goal forbids.
    """
    target = REPO / "core" / "scripts" / "dependent-unblock.py"
    assert target.is_file(), f"fixture source missing: {target}"
    src = target.read_text(encoding="utf-8")
    assert 'subprocess.run(["bash"' in src, "prose mention no longer present — retarget this test"
    assert GATE.scan_source(src, "core/scripts/dependent-unblock.py") == []


# --- 3. the override escape hatch -------------------------------------------

def test_same_line_marker_suppresses():
    src = 'subprocess.run(["bash", "x"])  # allow-bare-bash: posix-only shim\n'
    assert GATE.scan_source(src, "core/scripts/x.py") == []


def test_preceding_line_marker_suppresses():
    src = '# allow-bare-bash: posix-only shim\nsubprocess.run(["bash", "x"])\n'
    assert GATE.scan_source(src, "core/scripts/x.py") == []


def test_file_marker_suppresses_whole_file():
    src = '# allow-bare-bash-file: linux-only helper\nsubprocess.run(["bash", "a"])\nsubprocess.run(["bash", "b"])\n'
    assert GATE.scan_source(src, "core/scripts/x.py") == []


def test_unrelated_comment_does_not_suppress():
    """A marker-looking comment must be the real marker, not any comment."""
    src = '# bash is fine here honestly\nsubprocess.run(["bash", "x"])\n'
    assert GATE.scan_source(src, "core/scripts/x.py")


# --- 4. the sanctioned fix must stay clean ----------------------------------

@pytest.mark.parametrize("src", [
    'subprocess.run([BASH, "x.sh"])\n',                     # tests idiom
    'subprocess.run(bash_cmd("core/scripts/x.sh"))\n',      # production idiom
    'subprocess.run(["/bin/bash", "x.sh"])\n',              # explicit path, not bare
    'subprocess.run([sys.executable, "bash"])\n',           # "bash" not in argv[0]
    'subprocess.run(["git", "status"])\n',                   # unrelated argv
    'msg = "bash"\n',                                        # a bare string, no argv
])
def test_correct_and_unrelated_forms_are_clean(src):
    assert GATE.scan_source(src, "core/scripts/x.py") == []


def test_shell_true_without_bash_is_clean():
    assert GATE.scan_source('subprocess.run("ls -la", shell=True)\n', "core/scripts/x.py") == []


def test_bash_substring_command_is_not_flagged():
    """`bashful` must not match `bash` by prefix."""
    src = 'subprocess.run("bashful --x", shell=True)\n'
    assert GATE.scan_source(src, "core/scripts/x.py") == []


# --- 5. discriminating power (guard-1475) ----------------------------------

def test_detection_is_load_bearing(tmp_path):
    """Removing a detection rule MUST break the matching assertion.

    A test that passes with and without the code it claims to pin measures
    nothing. Each rule is deleted from a copy of the module and the copy is
    re-imported; the fixture that rule catches must stop being flagged.
    """
    original = GATE_PY.read_text(encoding="utf-8")

    # (i) forms a+b: neuter the list-literal rule.
    list_rule = 'if first.value in BARE_NAMES:'
    assert list_rule in original, "detection shape changed — retarget this test"
    broken = tmp_path / "no_list_rule.py"
    broken.write_text(original.replace(list_rule, "if False:"), encoding="utf-8")
    mod = _load(broken)
    assert mod.scan_source(FORM_A, "core/scripts/x.py") == [], \
        "form (a) still flagged with the list rule removed — the rule is not load-bearing"
    assert mod.scan_source(FORM_B, "core/scripts/x.py") == [], \
        "form (b) still flagged with the list rule removed — the rule is not load-bearing"

    # (ii) form c: neuter the shell-string rule.
    shell_rule = 'if lead is not None and self._starts_with_bare_bash(lead):'
    assert shell_rule in original, "detection shape changed — retarget this test"
    broken2 = tmp_path / "no_shell_rule.py"
    broken2.write_text(original.replace(shell_rule, "if False:"), encoding="utf-8")
    mod2 = _load(broken2)
    assert mod2.scan_source(FORM_C, "core/scripts/x.py") == [], \
        "form (c) still flagged with the shell rule removed — the rule is not load-bearing"

    # Control: the unmodified module still flags all three.
    for src in (FORM_A, FORM_B, FORM_C):
        assert GATE.scan_source(src, "core/scripts/x.py")


# --- scope + CLI surface ---------------------------------------------------

@pytest.mark.parametrize("rel,expected", [
    ("core/scripts/x.py", True),
    ("core/scripts/gates/x.py", True),
    ("core/scripts/tests/x.py", True),
    ("mind_api/src/x.py", True),            # depth 0 — the case `**` glob MISSED
    ("mind_api/src/endpoints/x.py", True),  # depth 1
    ("mind_api/src/meta/sub/x.py", True),   # depth 2 — pins real recursion
    ("world/scripts/x.py", True),
    ("agents/foxtrot/temp/x.py", False),
    ("docs/x.py", False),
    ("mind_api/tests/x.py", False),         # src only, per the goal's scope
    ("core/scripts/x.sh", False),           # .py only
])
def test_scope_globs(rel, expected):
    """Depth 0 and depth 2 are the load-bearing cases.

    `Path.match("mind_api/src/**/*.py")` degrades `**` to a single `*`, so it
    matched depth 1 only — silently excluding every top-level daemon module
    (agent_paths.py, lifecycle.py). An under-covering scope reads as covered,
    which is worse than no gate.
    """
    assert GATE._is_in_scope(rel) is expected


def test_detector_exempts_itself():
    """Else the gate could never be committed once it holds the pattern."""
    src = 'subprocess.run(["bash", "x"])\n'
    assert GATE.scan_source(src, "core/scripts/check-no-bare-bash.py") == []


def test_cli_paths_mode_reports_and_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(FORM_A, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(GATE_PY), "--paths", str(bad)],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert r.returncode == 1
    assert "HIT" in r.stderr


def test_cli_snippet_mode_blocks(tmp_path):
    r = subprocess.run(
        [sys.executable, str(GATE_PY), "--snippet"],
        input=FORM_A, capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert r.returncode == 1
    assert "BLOCKED" in r.stderr


def test_cli_snippet_mode_fails_open_on_unparseable():
    """Non-Python stdin is out of reach — never block on it."""
    r = subprocess.run(
        [sys.executable, str(GATE_PY), "--snippet"],
        input="this is not python (((", capture_output=True, text=True,
        cwd=str(REPO), timeout=120,
    )
    assert r.returncode == 0


def test_wrapper_runs_and_agrees_with_engine(tmp_path):
    """The .sh wrapper must reach the engine (it is what the hook invokes)."""
    bad = tmp_path / "bad.py"
    bad.write_text(FORM_A, encoding="utf-8")
    r = subprocess.run(
        [BASH, str(GATE_SH), "--paths", str(bad)],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert r.returncode == 1, f"wrapper did not reach the engine: {r.stderr}"


# --- pre-commit mode: added-lines scoping ---------------------------------

def _repo_with(tmp_path, rel: str, body: str, *, committed_first: str | None = None) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=tmp_path,
                   check=True, timeout=60)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, timeout=60)
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    if committed_first is not None:
        f.write_text(committed_first, encoding="utf-8")
        subprocess.run(["git", "add", rel], cwd=tmp_path, check=True, timeout=60)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True, timeout=60)
    f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=tmp_path, check=True, timeout=60)
    return tmp_path


def _precommit(repo: Path):
    return subprocess.run(
        [sys.executable, str(GATE_PY)],
        cwd=str(repo), capture_output=True, text=True, timeout=120,
    )


def test_precommit_blocks_a_newly_added_violation(tmp_path):
    repo = _repo_with(tmp_path, "core/scripts/new.py", FORM_A)
    r = _precommit(repo)
    assert r.returncode == 1, f"expected BLOCKED, got rc={r.returncode} {r.stderr}"
    assert "BLOCKED" in r.stderr
    assert "_runtime_bash" in r.stderr, "fix hint must name the sanctioned resolver"


def test_precommit_ignores_a_preexisting_violation(tmp_path):
    """Added-lines scoping, copied from check-no-python-cli-fallback.sh.

    ~18 bare-bash sites already exist in core/scripts/tests/*.py. Blocking on
    them would refuse every unrelated commit; the gate's job is to stop NEW
    introductions. --audit reports the pre-existing set.
    """
    repo = _repo_with(
        tmp_path, "core/scripts/old.py",
        FORM_A + "y = 2\n",          # violation already on line 2, plus a new line
        committed_first=FORM_A,      # ...and it was committed that way
    )
    r = _precommit(repo)
    assert r.returncode == 0, f"pre-existing site must not block: {r.stderr}"


def test_precommit_ignores_out_of_scope_paths(tmp_path):
    repo = _repo_with(tmp_path, "docs/scratch.py", FORM_A)
    assert _precommit(repo).returncode == 0
