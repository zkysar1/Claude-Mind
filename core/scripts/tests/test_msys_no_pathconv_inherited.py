"""Commit gates run with MSYS_NO_PATHCONV=1 inherited on a Windows loop commit ().

iteration-commit.sh, like every script that sources _platform.sh, exports
MSYS_NO_PATHCONV=1, and `git commit` hands that environment to the pre-commit
hook and to every gate it runs. With it set, MSYS stops rewriting /c/... argv
for native programs, so a gate that hands Windows python -- or `git -C` -- a
path it derived from `cd ... && pwd` breaks: python opens /c/x as C:\\c\\x, and
git reports "not a git repository". Measured on a Windows box: the domain-leak
scan refused to run on every loop commit and the ownership-flag gate's .py
detector never ran, while an interactive commit (variable unset) showed both
clean.

Each test drives the REAL gate from a throwaway repo with the variable SET and
asserts on the PAYLOAD -- the planted violation is reported -- not only on the
exit code. That matters for domain-leak-check.sh --staged: an unconverted
`git -C` does not fail loudly, it reads as "nothing staged" and prints CLEAN.

Off Windows the variable changes nothing, so there these are plain end-to-end
checks; on a Windows box they are the regression tests.

The same inheritance reaches outcome-observation-run.sh, which iteration-close.sh
calls at every deep close: its audit-log path reaches python only as an env
value, which MSYS stops translating once the variable is set (g-115-10698).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from _bash_helpers import BASH
from test_check_no_ownership_flag import GATE_SH, READ_GET, _repo_with

SCRIPTS = Path(__file__).resolve().parents[1]

# A synthetic term that exists only in the throwaway blocklist below.
TERM = "Quuxfrobnicator"


def _inherited_env() -> dict:
    """The loop-commit shape: the hook's environment carries the variable."""
    return {**os.environ, "MSYS_NO_PATHCONV": "1"}


def test_ownership_flag_gate_runs_its_py_detector_with_the_variable_inherited(tmp_path):
    repo = _repo_with(tmp_path, "core/scripts/probe.py", READ_GET)
    r = subprocess.run([BASH, str(GATE_SH)], cwd=str(repo), capture_output=True,
                       text=True, timeout=180, env=_inherited_env())
    out = r.stdout + r.stderr
    assert "can't open file" not in out, out
    assert ".py surface NOT checked" not in out, out
    assert r.returncode == 1, f"the staged .py read was not caught: {out}"


def test_domain_leak_staged_scan_runs_with_the_variable_inherited(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("domain-leak-check.sh", "_domain_leak_marker.py"):
        (scripts / name).write_bytes((SCRIPTS / name).read_bytes())
    (repo / "core" / "config").mkdir()
    # Bytes, not write_text: on Windows write_text emits CRLF, and the scanner
    # reads a CRLF blocklist term as "<term>\r", which matches nothing -- a
    # separate defect this test must not depend on.
    (repo / "core" / "config" / "domain-term-blocklist.txt").write_bytes(
        (TERM + "\n").encode("utf-8"))
    (scripts / "probe.sh").write_bytes(f"#!/usr/bin/env bash\necho {TERM}\n".encode("utf-8"))
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@example.invalid"],
                ["git", "config", "user.name", "t"],
                ["git", "add", "core/scripts/probe.sh"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True, timeout=60)
    r = subprocess.run([BASH, str(scripts / "domain-leak-check.sh"), "--staged"],
                       cwd=str(repo), capture_output=True, text=True, timeout=180,
                       env=_inherited_env())
    out = r.stdout + r.stderr
    assert "cannot resolve the exemption-marker predicate" not in out, out
    assert f"LEAK: '{TERM}'" in out, f"the staged term was not reported: {out}"
    assert r.returncode == 1, out


# Same hermetic strip as test_bash_edit_record.py: a fresh shell sourcing
# _paths.sh must resolve from the throwaway repo, not from leaked parent env.
_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
    "BODY_",
)


def test_outcome_observation_audit_line_lands_with_the_variable_inherited(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("outcome-observation-run.sh", "_paths.sh", "_platform.sh"):
        (scripts / name).write_bytes((SCRIPTS / name).read_bytes())
    # Pre-seeded so _paths.sh skips its python3 probe; exec'ing sys.executable
    # directly cannot loop through a py <-> python3 wrapper pair ().
    shim = scripts / ".python-shim"
    shim.mkdir()
    for name in ("python3", "python"):
        (shim / name).write_bytes(
            f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n'.encode("utf-8"))
    agent = repo / "agents" / "alpha"
    (agent / "session").mkdir(parents=True)
    (agent / "self.md").write_bytes(b"# alpha\n")
    (agent / "local-paths.conf").write_bytes(b"WORLD_PATH=\nMETA_PATH=\n")
    world, meta = tmp_path / "world", tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"}
    # No collector in this world, so the wrapper records exit 127 -- the audit
    # line is written either way, which is the part under test.
    env.update(MIND_AGENT="alpha", MIND_WORLD=world.as_posix(), MIND_META=meta.as_posix(),
               STORAGE_BACKEND="local", MSYS_NO_PATHCONV="1")
    r = subprocess.run(
        [BASH, (scripts / "outcome-observation-run.sh").as_posix(), "g-test-01", "deep"],
        cwd=str(repo), capture_output=True, text=True, timeout=120, env=env)
    out = r.stdout + r.stderr
    assert "Traceback" not in out, out
    log = repo / "core" / "logs" / "outcome-observation-runs.jsonl"
    assert log.is_file(), f"the audit line was lost: {out}"
    entries = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [e["goal_id"] for e in entries] == ["g-test-01"], out
