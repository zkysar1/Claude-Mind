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

It also reaches the git merge drivers (g-115-10806): iteration-push.sh runs
`git merge` with the variable inherited, and git spawns the registered driver in
that environment. A driver that cannot open its .py exits 2, and git reads any
non-zero driver exit as a content conflict, so a ledger changed on both sides
aborted the loop's push over a conflict that did not exist. The post-commit and
post-merge hooks inherit it too: their daemon-code predicate's `git -C` failed,
and it fails toward restart, so every Windows loop commit recycled the daemon.

pre-commit also runs session-manifest-gate.sh whenever the manifest is staged.
Its manifest path reached python only as an env value, so the gate answered
"manifest not found" and refused the loop's commit.

The domain suite inherits it as well, and its units extract embedded blocks
through extract-embedded-block.sh: the wrapper could not open its own .py, so
every such unit failed before it tested anything.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    for name in ("domain-leak-check.sh", "_domain_leak_marker.py", "_python_launcher.sh"):
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


# (driver, %P label, base, ours, theirs, a marker only ours has, one only theirs has)
_MERGE_DRIVERS = [
    ("git-merge-ayoai-ledger.sh", "agents/x/experience.jsonl",
     '{"id": "exp-base"}\n',
     '{"id": "exp-base"}\n{"id": "exp-O"}\n',
     '{"id": "exp-base"}\n{"id": "exp-T"}\n',
     "exp-O", "exp-T"),
    ("git-merge-append-ledger.sh", "core/config/replay-instrument-readings.md",
     "# readings\nbase line\n",
     "# readings\nbase line\nours reading A\n",
     "# readings\nbase line\ntheirs reading B\n",
     "ours reading A", "theirs reading B"),
    ("git-merge-journal-md.sh", "agents/x/journal/2026/09/2026-09-30.md",
     "",
     "## 2026-09-30 10:00 g-1\nours body A\n",
     "## 2026-09-30 11:00 g-2\ntheirs body B\n",
     "ours body A", "theirs body B"),
]


@pytest.mark.parametrize("driver,label,base,ours,theirs,ours_mark,theirs_mark",
                         _MERGE_DRIVERS, ids=[d[0] for d in _MERGE_DRIVERS])
def test_merge_driver_unions_with_the_variable_inherited(
        tmp_path, driver, label, base, ours, theirs, ours_mark, theirs_mark):
    # git's argv: %O %A %B %P. Bytes, so Windows cannot turn \n into \r\n.
    for name, text in (("O", base), ("A", ours), ("B", theirs)):
        (tmp_path / name).write_bytes(text.encode("utf-8"))
    r = subprocess.run([BASH, (SCRIPTS / driver).as_posix(), "O", "A", "B", label],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=120,
                       env=_inherited_env())
    out = r.stdout + r.stderr
    assert "can't open file" not in out, out
    assert r.returncode == 0, f"git would read rc={r.returncode} as a conflict: {out}"
    merged = (tmp_path / "A").read_text(encoding="utf-8")
    assert ours_mark in merged and theirs_mark in merged, merged


def test_daemon_code_predicate_answers_both_ways_with_the_variable_inherited(tmp_path):
    # post-commit and post-merge ask this predicate whether to recycle the
    # daemon. Its `git -C` failing reads as "changed" (fail toward restart), so
    # the docs-only commit is the case that went wrong ().
    repo = tmp_path / "repo"
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "mind_api" / "src").mkdir(parents=True)
    pred = repo / "core" / "scripts" / "mind-api-code-changed.sh"
    pred.write_bytes((SCRIPTS / "mind-api-code-changed.sh").read_bytes())
    (repo / "notes.md").write_bytes(b"one\n")
    (repo / "mind_api" / "src" / "app.py").write_bytes(b"X = 1\n")

    def commit(msg):
        for cmd in (["git", "add", "-A"], ["git", "commit", "-q", "-m", msg]):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True, timeout=60)

    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@example.invalid"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True, timeout=60)
    commit("base")

    def verdict():
        return subprocess.run([BASH, pred.as_posix(), "HEAD~1"], cwd=str(repo),
                              capture_output=True, text=True, timeout=120,
                              env=_inherited_env())

    (repo / "notes.md").write_bytes(b"two\n")
    commit("docs only")
    r = verdict()
    assert r.returncode == 1, f"a docs-only commit read as daemon code: {r.stdout}{r.stderr}"

    (repo / "mind_api" / "src" / "app.py").write_bytes(b"X = 2\n")
    commit("daemon code")
    r = verdict()
    assert r.returncode == 0, f"a daemon-code commit read as unchanged: {r.stdout}{r.stderr}"


def test_post_state_update_gate_python_handoffs_resolve_after_its_own_platform_sh():
    # This gate sources _platform.sh ITSELF, so on Windows it always runs in the
    # loop shape (). Run its real header, then its real ATTRIB_HELPER
    # assignment and the real SCRIPT_DIR value it hands the fresh-eyes heredoc,
    # and check python can open and import through both.
    gate = (SCRIPTS / "post-state-update-gate.sh").read_text(encoding="utf-8")
    header = gate.split('\nOUTCOME_CLASS="${1:-}"', 1)[0]
    # Under `bash -c` there is no BASH_SOURCE, so locate the scripts dir the
    # same way (cd + pwd, the MSYS /c/... form on Windows) from the cwd.
    self_locate = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    assert header.count(self_locate) == 1, "the gate's header changed shape"
    header = header.replace(self_locate, 'SCRIPT_DIR="$(pwd)"')
    attrib = next(ln for ln in gate.splitlines() if ln.startswith("ATTRIB_HELPER="))
    env_line = next(ln for ln in gate.splitlines()
                    if 'PROJECT_ROOT="$PROJECT_ROOT" SCRIPT_DIR=' in ln)
    heredoc_sd = env_line.split("SCRIPT_DIR=", 1)[1].split()[0]
    script = "\n".join([
        header,
        attrib,
        'printf "core/scripts/x.sh\\n" | $PYLAUNCH "$ATTRIB_HELPER" >/dev/null || exit 3',
        # From the repo root, as the loop runs it: from the scripts dir the
        # import would succeed off the cwd and hide a bad SCRIPT_DIR.
        f"(cd \"$PROJECT_ROOT\" && SCRIPT_DIR={heredoc_sd} python3 -c 'import os, sys; "
        "sys.path.insert(0, os.environ[\"SCRIPT_DIR\"]); import _fresh_eyes_signatures') || exit 4",
    ])
    r = subprocess.run([BASH, "-c", script], cwd=str(SCRIPTS), capture_output=True,
                       text=True, timeout=180, env={**os.environ, "MIND_AGENT": "alpha"})
    out = r.stdout + r.stderr
    assert r.returncode != 3, f"the attribution filter could not be run: {out}"
    assert r.returncode != 4, f"the fresh-eyes writer could not import its helper: {out}"
    assert r.returncode == 0, out


def test_session_manifest_gate_finds_its_manifest_with_the_variable_inherited():
    # pre-commit Gate 9 runs the gate with no arguments, so the default path is
    # the loop's shape: MANIFEST comes from _paths.sh's PROJECT_ROOT (the
    # /c/... form) and reaches python only through the environment
    # ().
    r = subprocess.run([BASH, (SCRIPTS / "session-manifest-gate.sh").as_posix()],
                       cwd=str(SCRIPTS.parents[1]), capture_output=True, text=True,
                       timeout=120, env=_inherited_env())
    out = r.stdout + r.stderr
    assert "manifest not found" not in out, out
    assert r.returncode == 0, out


def _msys_spelling(p: Path) -> str:
    """The Git Bash spelling of a path, /c/x for C:\\x. Unchanged off Windows."""
    if os.name != "nt":
        return p.as_posix()
    return "/" + p.drive[0].lower() + p.as_posix()[len(p.drive):]


@pytest.mark.parametrize("spelling", ["native", "msys"])
def test_embedded_block_extractor_runs_with_the_variable_inherited(tmp_path, spelling):
    # Domain-suite units call this wrapper with the variable inherited and
    # PROJECT_ROOT exported in the /c/... form their _paths.sh gave them
    # ( outcome 3). Two doors (rb-12388): the wrapper's own .py path,
    # and a /c/... --file argument, which only the callee can normalize.
    host = tmp_path / "host.sh"
    # No quote in the marker: MSYS bash re-parses its Windows command line, and
    # a lone ' there swallows the arguments after it.
    host.write_text("cat <<MARK\necho inside-the-block\nMARK\n", encoding="utf-8")
    env = {**_inherited_env(), "PROJECT_ROOT": _msys_spelling(SCRIPTS.parents[1])}
    arg = host.as_posix() if spelling == "native" else _msys_spelling(host)
    r = subprocess.run([BASH, (SCRIPTS / "extract-embedded-block.sh").as_posix(),
                        "--grammar", "shell", "--file", arg,
                        "--open-marker", "<<MARK", "--close-line", "MARK"],
                       capture_output=True, text=True, timeout=120, env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert r.stdout.strip() == "echo inside-the-block", out
