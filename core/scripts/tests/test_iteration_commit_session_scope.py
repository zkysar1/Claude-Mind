"""test_iteration_commit_session_scope.py — regression test for .

encode-session Phase Final.5 commits with a synthetic --goal-id
(encode-session). In assistant mode no team-state in_flight row names that
goal, so the pre-claim mtime filter is inert, and every partner filter keys on
OTHER agents. Nothing separated two sessions of the SAME agent sharing one
checkout. Measured 2026-09-27 (alpha, assistant session b5b3ea83, two sibling
sessions bound to the same checkout): Final.5 staged 25 paths, its own 2 plus
23 a sibling session was still editing. Those were rules files, CLAUDE.md and
two config files bulk-edited by a command, plus 5 new config files. Only a
commit-msg refusal stopped the commit, and the refusal then left all 25 staged
in the shared index.

Fix under test:
  - both edit recorders stamp each record with the id of the session that made
    the edit (`sid`);
  - `iteration-commit.sh --session-sid <SID>` commits a path outside
    agents/<agent>/ only when a record carries that SID. The agent's own
    agents/<agent>/ churn is unchanged, and every path left out is listed;
  - a REFUSED session-scoped commit unstages exactly what that invocation
    staged, so the shared index is left as it was found (guard-5824 harm 1);
  - Final.5 passes `--session-sid "$MIND_SID"` and no longer claims the
    script filters same-agent partner WIP.

g-115-11374 (the stamp was the wrong session's): the Bash recorder now credits
a change to the one session whose command was running when it happened
(_bash_inflight.py windows), and --session-sid leaves out a path this session
changed when another writer also changed it after its last commit. The
end-to-end test at the bottom runs the real recorder into the real commit in
the measured incident's shape.

Outcomes 1 and 2 run the Final.5 invocation READ OUT OF THE LIVE SKILL.md, not
a hand-copied argv (guard-920: replicate the production call shape). So the
test goes red if the skill ever drops the scope, and it is red against
80a5ddae4a, where the skill had no scope and the script had no flag.

Pattern: subprocess + tempdir + scripted git init, the same shim mechanics as
test_iteration_commit_gate_refusal.py; the recorder harness mirrors
test_bash_edit_record.py.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO_ROOT = SCRIPT_DIR.parents[2]
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "encode-session" / "SKILL.md"

PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_session_scope_test"

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(CORE_SCRIPTS))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402
import _bash_inflight  # noqa: E402

# Strip the framework env namespace so a leaked MIND_SID / MIND_AGENT /
# STORAGE_BACKEND from the running session cannot steer the script under test
# (same prefixes as test_bash_edit_record.py).
_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
    "BODY_",
)

SID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"  # the session running Final.5
SID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"  # a sibling session, same agent

# The invoking session's work: one Edit-tool edit, one command-created file
# (the Bash recorder stamps it), and the agent's own agent-dir churn.
A_PATHS = {
    ".claude/rules/mine.md",
    "core/scripts/mine-tool.py",
    "agents/alpha/skill-invocations.jsonl",
}
# The sibling's work in progress, in the incident's shape: one recorded
# Edit-tool edit, two files bulk-edited by a command that no recorder saw
# (CLAUDE.md is outside the Bash recorder's core/ + .claude/ scan), one new
# recorded file.
B_PATHS = {
    ".claude/rules/theirs.md",
    "CLAUDE.md",
    "core/config/arch.md",
    "core/config/new-convention.md",
}


def _hermetic_env(**overrides) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"
    }
    env.update(overrides)
    return env


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _git(repo: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, timeout=15,
                          capture_output=True, text=True).stdout


def _setup_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    for a in ("alpha", "zeta"):
        d = repo / "agents" / a
        (d / "session").mkdir(parents=True)
        (d / "self.md").write_text(f"# {a}\n")
    (repo / "agents" / "alpha" / "skill-invocations.jsonl").write_text("{}\n")
    for rel in (".claude/rules/mine.md", ".claude/rules/theirs.md",
                "CLAUDE.md", "core/config/arch.md", "core/scripts/.gitkeep"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# base\n")
    # The real repo ignores every agent's session/ dir, which is where the
    # edit log and commit-refused.json live.
    (repo / ".gitignore").write_text("**/session/\n__pycache__/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _shim_iteration_commit(tmp: Path) -> Path:
    shim_dir = tmp / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    # iteration-commit.sh sources _python_launcher.sh beside itself ().
    launcher = ITERATION_COMMIT_SH.parent / "_python_launcher.sh"
    (shim_dir / launcher.name).write_bytes(launcher.read_bytes())
    # No in_flight row anywhere: the assistant-mode Final.5 condition.
    ts = shim_dir / "team-state-read.sh"
    ts.write_text("#!/usr/bin/env bash\n# null shim\necho null\nexit 0\n")
    ts.chmod(0o755)
    return target


def _write(repo: Path, rel: str, body: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _record(repo: Path, rel: str, sid: str | None, mtime: int | None = None,
            candidates: list | None = None) -> None:
    """Append one edit record, shaped as the recorders write it."""
    rec = {"file": rel, "mtime": int(time.time()) if mtime is None else mtime,
           "edit_ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "goal_id": ""}
    if sid is not None:
        rec["sid"] = sid
    if candidates:
        rec["candidates"] = candidates
    log = repo / "agents" / "alpha" / "session" / "uncommitted-edits.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _seed_two_sessions(repo: Path) -> None:
    # Session A (invoking Final.5).
    _write(repo, ".claude/rules/mine.md", "# base\nmine\n")
    _record(repo, ".claude/rules/mine.md", SID_A)
    _write(repo, "core/scripts/mine-tool.py", "print('mine')\n")
    _record(repo, "core/scripts/mine-tool.py", SID_A)
    _write(repo, "agents/alpha/skill-invocations.jsonl", "{}\n{\"skill\":\"encode-session\"}\n")
    # Session B (sibling, same agent, still mid-work).
    _write(repo, ".claude/rules/theirs.md", "# base\nhalf-done\n")
    _record(repo, ".claude/rules/theirs.md", SID_B)
    _write(repo, "CLAUDE.md", "# base\nbulk edit\n")          # unrecorded
    _write(repo, "core/config/arch.md", "# base\nbulk edit\n")  # unrecorded
    _write(repo, "core/config/new-convention.md", "# new\n")
    _record(repo, "core/config/new-convention.md", SID_B)


def _final5_section() -> str:
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.index("### Phase Final.5")
    end = text.find("\n### ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _final5_args() -> list[str]:
    """The argv Final.5 hands iteration-commit.sh, parsed from the skill."""
    lines = _final5_section().splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("Bash:") and "iteration-commit.sh" in line:
            parts = []
            j = i
            while True:
                cur = lines[j].rstrip()
                cont = cur.endswith("\\")
                parts.append(cur[:-1] if cont else cur)
                if not cont:
                    break
                j += 1
            return shlex.split(" ".join(parts).split("iteration-commit.sh", 1)[1])
    raise AssertionError("Final.5 has no `Bash: ... iteration-commit.sh` invocation")


def _bind(args: list[str], repo: Path, sid: str) -> list[str]:
    values = {"$PROJECT_ROOT": str(repo), "$MIND_SID": sid}
    out = [values.get(a, a) for a in args]
    unbound = [a for a in out if "$" in a]
    assert not unbound, f"Final.5 invocation uses variables this test does not bind: {unbound}"
    return out


def _run_commit(shim: Path, args: list[str], agent: str | None = "alpha"):
    env = _hermetic_env(MIND_AGENT=agent) if agent else _hermetic_env()
    return subprocess.run([GIT_BASH] + [_to_bash_path(a) for a in [shim, *args]],
                          env=env, capture_output=True, text=True, timeout=60)


def _committed(repo: Path) -> set[str]:
    return {p for p in _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() if p}


def _staged(repo: Path) -> set[str]:
    return {p for p in _git(repo, "diff", "--cached", "--name-only").splitlines() if p}


def _dirty(repo: Path) -> set[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return {line[3:] for line in out.splitlines() if line}


def _refusing_hook(repo: Path) -> None:
    h = repo / ".git" / "hooks" / "commit-msg"
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("#!/bin/sh\necho 'commit-msg gate: refused for test' >&2\nexit 1\n")
    h.chmod(0o755)


# --------------------------------------------------------------------------- #
# Outcome 1 — the Final.5 commit holds only the invoking session's paths
# --------------------------------------------------------------------------- #

def test_final5_commit_contains_only_the_invoking_sessions_paths():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _seed_two_sessions(repo)
        head0 = _git(repo, "rev-parse", "HEAD").strip()

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
        assert _git(repo, "rev-parse", "HEAD").strip() != head0, f"no commit made:\n{r.stderr}"
        committed = _committed(repo)
        assert committed == A_PATHS, (
            f"Final.5 committed another session's paths: {sorted(committed - A_PATHS)}; "
            f"missing own: {sorted(A_PATHS - committed)}\n{r.stderr}")
        # The sibling's work is untouched: still in the working tree, not staged.
        assert B_PATHS <= _dirty(repo), sorted(B_PATHS - _dirty(repo))
        assert not (_staged(repo) & B_PATHS), sorted(_staged(repo))
        # Never silent: every path left out is named.
        for p in B_PATHS:
            assert f"not-this-session): {p}" in r.stderr, f"{p} left out without being listed:\n{r.stderr}"


def test_positive_control_the_same_seed_is_swept_without_the_scope():
    """guard-4166: prove the seed exercises the incident. With the scope
    stripped from the same Final.5 argv, the sibling's paths ARE committed."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _seed_two_sessions(repo)
        args = _bind(_final5_args(), repo, SID_A)
        if "--session-sid" in args:
            k = args.index("--session-sid")
            args = args[:k] + args[k + 2:]

        r = _run_commit(shim, args)

        assert r.returncode == 0, r.stderr
        assert B_PATHS <= _committed(repo), f"seed no longer reproduces the sweep:\n{r.stderr}"


def test_a_new_directory_is_split_by_session_not_committed_whole():
    """Porcelain collapses a new untracked directory to one entry while the
    log records files. The scope must decide per FILE, so a directory both
    sessions wrote into does not ride in whole on one session's record."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _write(repo, "core/newskill/SKILL.md", "# mine\n")
        _record(repo, "core/newskill/SKILL.md", SID_A)
        _write(repo, "core/newskill/draft.md", "# theirs\n")
        _record(repo, "core/newskill/draft.md", SID_B)

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 0, r.stderr
        assert _committed(repo) == {"core/newskill/SKILL.md"}, r.stderr
        assert "core/newskill/draft.md" in _dirty(repo)


def test_a_record_without_a_session_id_is_not_this_sessions():
    """A legacy record (written before records carried `sid`) proves an edit by
    SOME session of the agent, not by this one, so the scope leaves it out."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _write(repo, "core/scripts/legacy.py", "print(1)\n")
        _record(repo, "core/scripts/legacy.py", None)

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 0, r.stderr
        assert "core/scripts/legacy.py" in _dirty(repo)
        assert "not-this-session): core/scripts/legacy.py" in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
#  — a file another writer also changed is not this session's alone
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("other_sid, candidates, label", [
    (SID_B, None, SID_B[:8]),
    ("", [SID_A, SID_B], "ambiguous"),
    (None, None, "unattributed"),
])
def test_a_file_another_writer_also_changed_is_left_out_and_named(other_sid, candidates, label):
    """git stages whole files: committing arch.md for A would ship the other
    writer's change too, so the scope leaves it out and says who else wrote it."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _write(repo, ".claude/rules/mine.md", "# base\nmine\n")
        _record(repo, ".claude/rules/mine.md", SID_A)
        _write(repo, "core/config/arch.md", "# base\nmine\ntheirs\n")
        _record(repo, "core/config/arch.md", other_sid, candidates=candidates)
        _record(repo, "core/config/arch.md", SID_A)

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 0, r.stderr
        assert _committed(repo) == {".claude/rules/mine.md"}, r.stderr
        assert "core/config/arch.md" in _dirty(repo)
        assert f"also-changed-by-another): core/config/arch.md (other: {label})" in r.stderr, r.stderr


def test_a_row_older_than_the_files_last_commit_does_not_contest():
    """The other writer's change is already in HEAD, so it does not block this
    session's later change: a ledger keeps rows for files committed by pathspec."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        committed_at = int(_git(repo, "log", "-1", "--format=%ct").strip())
        _record(repo, "core/config/arch.md", SID_B, mtime=committed_at - 100)
        _write(repo, "core/config/arch.md", "# base\nmine\n")
        _record(repo, "core/config/arch.md", SID_A)

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 0, r.stderr
        assert _committed(repo) == {"core/config/arch.md"}, r.stderr


def _open_window(repo: Path, sid: str, command: str, start: float) -> None:
    """The window bash-agent-inject opens before a command, with a chosen start."""
    d = repo / "agents" / "alpha" / "sessions" / sid / _bash_inflight.DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / _bash_inflight.command_key(command)).write_text(
        json.dumps({"start": start, "expires": time.time() + 600}))


def _command_returns(repo: Path, sid: str, command: str) -> None:
    """The recorder as the PostToolUse of `command` in session `sid`."""
    r = subprocess.run(
        [GIT_BASH, _to_bash_path(repo / "core" / "scripts" / "bash-edit-record.sh")],
        input=json.dumps({"session_id": sid, "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=30, env=_hermetic_env(MIND_AGENT="alpha"))
    assert r.returncode == 0, r.stderr


def _write_at(repo: Path, rel: str, body: str, mtime: int) -> None:
    _write(repo, rel, body)
    os.utime(repo / rel, (mtime, mtime))


def test_end_to_end_recorder_then_commit_in_the_incident_shape():
    """Both directions of , through the real recorder and the real
    commit, with the Final.5 argv read from the skill.

    1. Session B's long command writes core/scripts/theirs.py; session A's
       command starts after that write and ends first, so A's recorder records
       it (the 2026-09-28 e7311258 shape). It must be credited to B.
    2. A's next command edits core/scripts/mine.py, whose only earlier row is a
       stale August row with no sid (the 2026-09-29 06a996f3 shape). Dedup on the
       file alone dropped the new change; it must be recorded as A's.
    So A's commit holds mine.py and not theirs.py, and B's commit holds
    theirs.py."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        core_scripts = repo / "core" / "scripts"
        for fname in ("bash-edit-record.sh", "_paths.sh", "_bash_inflight.py"):
            (core_scripts / fname).write_bytes((CORE_SCRIPTS / fname).read_bytes())
            (core_scripts / fname).chmod(0o755)
        shim_dir = core_scripts / ".python-shim"
        shim_dir.mkdir()
        for name in ("python3", "python"):  # sys.executable, never `py -3` ()
            (shim_dir / name).write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
            (shim_dir / name).chmod(0o755)
        (repo / "agents" / "alpha" / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
        with (repo / ".gitignore").open("a") as fh:
            fh.write("**/sessions/\n.python-shim/\ncore/.pycache/\n")
        _write_at(repo, "core/scripts/mine.py", "print(0)\n", int(time.time()) - 1000)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "recorders")
        base = int(time.time())
        session = repo / "agents" / "alpha" / "session"
        (session / "uncommitted-edits.jsonl").write_text(json.dumps(
            {"file": "core/scripts/mine.py", "mtime": base - 5_000_000,
             "edit_ts": "2026-08-01T00:00:00", "goal_id": ""}) + "\n")
        cmd_a1, cmd_a2, cmd_b = "python3 retrieve-loop.py", "sed -i s/0/1/ core/scripts/mine.py", "python3 write-tests.py"

        _open_window(repo, SID_B, cmd_b, base - 50)
        _write_at(repo, "core/scripts/theirs.py", "print('theirs')\n", base - 40)
        (session / ".bash-edit-cursor").write_text(f"{base - 60}\n")
        _open_window(repo, SID_A, cmd_a1, base - 35)
        _command_returns(repo, SID_A, cmd_a1)
        _command_returns(repo, SID_B, cmd_b)
        (session / ".bash-edit-cursor").write_text(f"{base - 15}\n")
        _open_window(repo, SID_A, cmd_a2, base - 12)
        _write_at(repo, "core/scripts/mine.py", "print('mine')\n", base - 10)
        _command_returns(repo, SID_A, cmd_a2)

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))
        assert r.returncode == 0, r.stderr
        assert _committed(repo) == {"core/scripts/mine.py"}, r.stderr
        assert "not-this-session): core/scripts/theirs.py" in r.stderr, r.stderr

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_B))
        assert r.returncode == 0, r.stderr
        assert _committed(repo) == {"core/scripts/theirs.py"}, r.stderr


def test_empty_session_id_fails_loud_instead_of_sweeping():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _seed_two_sessions(repo)
        head0 = _git(repo, "rev-parse", "HEAD").strip()

        r = _run_commit(shim, _bind(_final5_args(), repo, ""))

        assert r.returncode == 1, f"rc={r.returncode}\n{r.stderr}"
        assert "--session-sid" in r.stderr, r.stderr
        assert _git(repo, "rev-parse", "HEAD").strip() == head0
        assert not _staged(repo)


# --------------------------------------------------------------------------- #
# Outcome 2 — a refused Final.5 commit leaves no other session's path staged
# --------------------------------------------------------------------------- #

def test_refused_final5_commit_leaves_no_other_session_path_staged():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _seed_two_sessions(repo)
        _refusing_hook(repo)
        head0 = _git(repo, "rev-parse", "HEAD").strip()

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 2, f"rc={r.returncode}\n{r.stderr}"
        assert _git(repo, "rev-parse", "HEAD").strip() == head0, "HEAD moved"
        staged = _staged(repo)
        assert not (staged & B_PATHS), f"another session's paths left staged: {sorted(staged & B_PATHS)}"
        # The scope also undoes its OWN staging: nothing is left in the shared
        # index for a sibling's commit or merge to absorb (guard-5824 harm 1).
        assert staged == set(), f"index not restored: {sorted(staged)}\n{r.stderr}"
        assert "RESTORED" in r.stderr, r.stderr
        # The work itself is intact in the working tree.
        assert A_PATHS | B_PATHS <= _dirty(repo)


def test_refusal_does_not_unstage_what_this_invocation_did_not_stage():
    """guard-741: never discard another session's deliberate staging. A path
    the sibling staged BEFORE Final.5 ran stays staged; only the invocation's
    own staging is undone."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _seed_two_sessions(repo)
        _git(repo, "add", "--", ".claude/rules/theirs.md")  # the sibling's own staging
        _refusing_hook(repo)

        r = _run_commit(shim, _bind(_final5_args(), repo, SID_A))

        assert r.returncode == 2, r.stderr
        assert _staged(repo) == {".claude/rules/theirs.md"}, sorted(_staged(repo))


# --------------------------------------------------------------------------- #
# Outcome 3 — the skill no longer claims same-agent filtering
# --------------------------------------------------------------------------- #

def test_final5_text_no_longer_claims_the_script_filters_partner_wip():
    section = _final5_section()
    assert "partner WIP (cross-agent mtime filter)" not in section
    assert "--session-sid" in _final5_args(), "Final.5 commit is not session-scoped"


# --------------------------------------------------------------------------- #
# The recorders stamp the session id the scope keys on
# --------------------------------------------------------------------------- #

def _setup_recorder_repo(tmp: Path) -> Path:
    """Temp repo whose core/scripts holds the real recorders + _paths.sh, so
    _paths.sh anchors PROJECT_ROOT to it (test_bash_edit_record.py harness)."""
    repo = tmp / "repo"
    for a in ("alpha", "zeta"):
        d = repo / "agents" / a
        (d / "session").mkdir(parents=True)
        (d / "self.md").write_text(f"# {a}\n")
        (d / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (repo / ".claude").mkdir()
    for fname in ("uncommitted-edits-record.sh", "bash-edit-record.sh", "_paths.sh",
                  "_python_launcher.sh", "_bash_inflight.py"):
        dst = core_scripts / fname
        dst.write_bytes((CORE_SCRIPTS / fname).read_bytes())
        dst.chmod(0o755)
    ts = core_scripts / "team-state-read.sh"
    ts.write_text("#!/usr/bin/env bash\necho null\nexit 0\n")
    ts.chmod(0o755)
    # sys.executable, never `py -3`: see test_bash_edit_record.py ().
    shim_dir = core_scripts / ".python-shim"
    shim_dir.mkdir()
    for name in ("python3", "python"):
        s = shim_dir / name
        s.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
        s.chmod(0o755)
    return repo


def _log_records(repo: Path) -> list[dict]:
    log = repo / "agents" / "alpha" / "session" / "uncommitted-edits.jsonl"
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


def test_edit_recorder_stamps_the_payload_session_id():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_recorder_repo(Path(td))
        target = repo / "core" / "scripts" / "edited.py"
        target.write_text("# edited\n")
        payload = json.dumps({"session_id": SID_A,
                              "tool_input": {"file_path": str(target).replace("\\", "/")}})

        r = subprocess.run(
            [GIT_BASH, _to_bash_path(repo / "core" / "scripts" / "uncommitted-edits-record.sh")],
            input=payload, capture_output=True, text=True, timeout=30,
            env=_hermetic_env(MIND_AGENT="alpha"))

        assert r.returncode == 0, r.stderr
        recs = [x for x in _log_records(repo) if x.get("file") == "core/scripts/edited.py"]
        assert recs, f"edit not recorded: {_log_records(repo)}"
        assert recs[-1].get("sid") == SID_A, recs


def test_bash_recorder_stamps_the_session_whose_command_was_running():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_recorder_repo(Path(td))
        base = int(time.time())
        (repo / "agents" / "alpha" / "session" / ".bash-edit-cursor").write_text(f"{base - 60}\n")
        _open_window(repo, SID_A, "python3 make.py", base - 20)
        made = repo / "core" / "scripts" / "made-by-command.py"
        made.write_text("# x\n")
        os.utime(made, (base - 5, base - 5))

        r = subprocess.run(
            [GIT_BASH, _to_bash_path(repo / "core" / "scripts" / "bash-edit-record.sh")],
            input=json.dumps({"session_id": SID_A, "tool_input": {"command": "python3 make.py"}}),
            capture_output=True, text=True, timeout=30, env=_hermetic_env(MIND_AGENT="alpha"))

        assert r.returncode == 0, r.stderr
        recs = [x for x in _log_records(repo) if x.get("file") == "core/scripts/made-by-command.py"]
        assert recs, f"command-made file not recorded: {_log_records(repo)}"
        assert recs[-1].get("sid") == SID_A, recs


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
