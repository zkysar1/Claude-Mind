"""test_skill_evaluate_judge_wiring.py --  (asp-306).

Pins the WIRING of judge provenance, not the function that computes it.

g-306-394 shipped judge_model/harness with 15 tests, all green, and the field
was wrong on every record the fleet wrote. Each of those tests exercised
_judge_provenance() as a FUNCTION against a manipulated os.environ. None asked
whether the caller's environment can REACH the writer -- and it cannot: under
daemon-only architecture the writer is the long-lived daemon, which inherits
the environment of whichever session spawned it and holds it for its whole
lifetime. Measured 2026-09-01, daemon pid 505894: MIND_JUDGE_MODEL absent (so
judge_model was "unknown" for every request) and CLAUDECODE present (so harness
was stamped "claude-code" on every evaluation, forever). Absent would have been
honest; that is confidently WRONG (guard-2480, the guard-1925 hazard).

guard-1943: a green suite certifies the FUNCTION, never the WIRING. So every
test here crosses at least one real process or transport boundary:

  1. THE REGRESSION PIN -- the daemon writer, called with no judge arguments
     while the judge env vars ARE set in its process, must record "unknown".
     This is the mutation-proof that the environment read is gone; it fails if
     anyone reintroduces one.
  2. Source-level: the daemon module reads none of the judge variables.
  3. END-TO-END through each REAL wrapper against a REAL daemon: an exported
     MIND_JUDGE_MODEL must arrive in the recorded evaluation.
  4. BOTH entry points into the writer, because there are two and the goal
     named only one. skill-evaluate.sh is the direct route;
     skill-quality-score.sh is the Step 8.76 per-goal call site and therefore
     the dominant producer of evaluations. Fixing only the first would have
     left every loop-written record carrying the daemon's environment
     (guard-3448: a gate is only as broad as its entry points).

Run: py -3 -m pytest core/scripts/tests/test_skill_evaluate_judge_wiring.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO = CORE_SCRIPTS.parent.parent
DAEMON_SRC = REPO / "mind_api" / "src" / "meta" / "skill_evaluate.py"

sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare 'bash' argv[0])

JUDGE_ENV = ("MIND_JUDGE_MODEL", "CLAUDECODE", "ZAKCODE_MODEL",
             "ZAKCODE_SESSION", "CLAUDE_CODE_SUBAGENT_MODEL")
SKILL = "aspirations-state-update"


def _load_daemon_module():
    """Import the daemon twin as a PACKAGE module.

    Not spec_from_file_location: the daemon twin uses relative imports
    (`from ..agent_paths import ...`), which raise ImportError when a module is
    loaded by path with no parent package. The package import is also the shape
    the daemon itself uses, so this exercises the real module object rather
    than a second copy of it.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from mind_api.src.meta import skill_evaluate
    return skill_evaluate


# --- 1: THE REGRESSION PIN --------------------------------------------------

def test_writer_ignores_its_own_environment(monkeypatch):
    """The defect, stated as a test.

    The judge env vars are SET in this process -- exactly the daemon's
    situation, which carries its spawning session's environment. The writer is
    called the way an un-forwarding caller would call it. It must record
    "unknown" (absent, honest) and never the ambient values (present, wrong).
    """
    monkeypatch.setenv("MIND_JUDGE_MODEL", "ambient-model-must-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    mod = _load_daemon_module()
    assert mod._judge_provenance() == ("unknown", "unknown")


def test_writer_prefers_supplied_values_over_ambient(monkeypatch):
    monkeypatch.setenv("MIND_JUDGE_MODEL", "ambient-model-must-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    mod = _load_daemon_module()
    assert mod._judge_provenance("supplied-model", "zakcode") == (
        "supplied-model", "zakcode")


# --- 2: source-level --------------------------------------------------------

def test_daemon_module_reads_no_judge_env():
    """Belt and braces for test 1: the read forms must not appear at all.

    Anchored on the VARIABLE NAMES rather than on `os.environ`, so a switch to
    os.getenv or a direct subscript cannot slip past.
    """
    text = DAEMON_SRC.read_text(encoding="utf-8")
    for var in JUDGE_ENV:
        for form in ('os.environ.get("%s"' % var, "os.environ.get('%s'" % var,
                     'os.environ["%s"' % var, "os.environ['%s'" % var,
                     'os.getenv("%s"' % var, "os.getenv('%s'" % var):
            assert form not in text, (var, form)


# --- 3 + 4: end-to-end through each real wrapper ----------------------------

def _seed_skill(project_root: Path) -> None:
    """skill-quality-score canonicalizes against .claude/skills/<name>."""
    d = project_root / ".claude" / "skills" / SKILL
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: %s\n---\n" % SKILL, encoding="utf-8")


def _wrapper_env(df, judge_model=None, harness_flag=None):
    env = dict(os.environ)
    for key in JUDGE_ENV:
        env.pop(key, None)
    env["RT_DIR"] = str(df.runtime_dir)
    env["MIND_AGENT"] = "alpha"
    env["STORAGE_BACKEND"] = "local"   # guard-955
    if judge_model:
        env["MIND_JUDGE_MODEL"] = judge_model
    if harness_flag:
        env[harness_flag] = "1"
    return env


def _recorded_entry(project_root: Path):
    path = project_root / "meta" / "skill-quality.yaml"
    assert path.exists(), "wrapper did not reach the writer: %s absent" % path
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data["skills"][SKILL]["evaluations"][-1]


def _run(cmd, env):
    r = subprocess.run(cmd, cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, "rc=%s\nstdout=%s\nstderr=%s" % (
        r.returncode, r.stdout, r.stderr)
    return r


def _evaluate_cmd(goal):
    return [BASH, (CORE_SCRIPTS / "skill-evaluate.sh").as_posix(), "score",
            "--skill", SKILL, "--goal", goal,
            "--safety", "good", "--completeness", "good",
            "--executability", "good", "--maintainability", "good",
            "--cost-awareness", "good"]


def _quality_cmd(goal):
    return [BASH, (CORE_SCRIPTS / "skill-quality-score.sh").as_posix(), "score",
            "--skill", SKILL, "--goal", goal,
            "--outcomes-met", "2", "--outcomes-total", "2",
            "--episode-chain-count", "0", "--guardrail-violations", "0",
            "--cost-awareness", "good"]


@pytest.fixture()
def daemon():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir(parents=True, exist_ok=True)
        with DaemonFixture(world) as df:
            _seed_skill(df.project_root)
            yield df


@pytest.mark.parametrize("which,build", [
    ("skill-evaluate.sh", _evaluate_cmd),
    ("skill-quality-score.sh", _quality_cmd),   # the Step 8.76 dominant path
])
def test_exported_judge_model_reaches_recorded_evaluation(daemon, which, build):
    """The wiring, end to end: exported env -> wrapper -> daemon -> YAML.

    This is the assertion the 15 function-level tests could not make. It
    crosses a process boundary and an HTTP boundary, so it fails if the
    wrapper stops resolving, stops forwarding, or the writer stops reading
    what it is sent.
    """
    _run(build("g-wire-1"),
         _wrapper_env(daemon, judge_model="claude-opus-5",
                      harness_flag="CLAUDECODE"))
    entry = _recorded_entry(daemon.project_root)
    assert entry["judge_model"] == "claude-opus-5", which
    assert entry["harness"] == "claude-code", which


@pytest.mark.parametrize("which,build", [
    ("skill-evaluate.sh", _evaluate_cmd),
    ("skill-quality-score.sh", _quality_cmd),
])
def test_unresolvable_judge_records_unknown_not_the_daemons_env(
        daemon, which, build):
    """The honest-absent case, and the one that would have caught the defect.

    The caller has no judge identity, so the record must say "unknown". The
    daemon serving this request DOES carry a judge environment (the fixture
    inherited this test process's), so any value other than "unknown" here is
    the daemon's own environment leaking into the record.
    """
    env = _wrapper_env(daemon)
    os.environ["CLAUDECODE"] = "1"          # ambient in the daemon's process
    try:
        _run(build("g-wire-2"), env)
    finally:
        os.environ.pop("CLAUDECODE", None)
    entry = _recorded_entry(daemon.project_root)
    assert entry["judge_model"] == "unknown", which
    assert entry["harness"] == "unknown", which


def test_both_wrappers_resolve_judge_provenance():
    """Cheap structural backstop for the two call sites above.

    The end-to-end tests are the real evidence; this one names the omission
    directly, so a wrapper that silently loses the call fails with a message
    that says which one.
    """
    for name in ("skill-evaluate.sh", "skill-quality-score.sh"):
        text = (CORE_SCRIPTS / name).read_text(encoding="utf-8")
        assert "rt_judge_provenance" in text, name
        assert "judge_model" in text, name
        assert "harness" in text, name
