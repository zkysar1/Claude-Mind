"""test_iteration_commit_gate_refusal.py —  regression test.

A commit-msg gate refusal is DETERMINISTIC: the same input refuses identically
every time. iteration-commit.sh nonetheless spent its whole 3-attempt retry
budget on it, printed ONE stderr line inside iteration-close's long output, and
left the index STAGED. Every subsequent iteration-push then hit
`_selfheal_cross_agent_churn_remerge` and deferred under guard-741 — correctly by
its own logic, forever. Measured 2026-09-12 (foxtrot, LAPTOP-3IOFCNEO): 9 defers
over 1h47m, ending 52 commits behind, with the INTEGRATE-DEFER STREAK alarm
naming three causes and the actual one not among them.

THE FIX IS AN INVERTED PREDICATE, and these tests are written to pin the
inversion rather than the symptom: retry ONLY on a known-transient signature and
treat every other non-zero rc as deterministic. That is gate-agnostic by
construction — a NEW commit-msg gate inherits the handling for free because it
simply is not on the transient whitelist, and no gate's message is ever matched
(guard-3878: the predicate also runs only on the failure branch, so it can never
match output the success path emits).

THE POSITIVE CONTROL IS LOAD-BEARING (guard-4166). Tests 1/2/4 are all satisfied
by a degenerate "never retry anything" implementation, which would regress the
lock-contention recovery this loop exists for. `test_transient_failure_still_
retries_and_can_succeed` fails against that version, and tests 1-2 fail against
the pre-fix "retry everything" version — so neither extreme passes the file.

Cross-references: g-115-9807 (this fix), g-115-1673 (stale-lock auto-recovery,
whose signature list is the one source of truth reused here), guard-741 (the
downstream defer), guard-3878 (error-detector predicates), rb-9071 (the
two-arm-vs-three-arm discriminator question, considered and decided in the code).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"
ITERATION_PUSH_SH = CORE_SCRIPTS / "iteration-push.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_gate_refusal_test"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _run_bash(args, env=None, timeout=60):
    cmd = [BASH] + [_to_bash_path(a) for a in args]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, env=full_env, capture_output=True, text=True, timeout=timeout)


def _git(repo: Path, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, timeout=15)


def _setup_repo(tmpdir: Path, agents=("alpha", "zeta")) -> Path:
    repo = tmpdir / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "agents").mkdir()
    for a in agents:
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
        (d / "session").mkdir()
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    (repo / ".gitignore").write_text("__pycache__/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _shim_iteration_commit(tmpdir: Path) -> Path:
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    ts = shim_dir / "team-state-read.sh"
    ts.write_text("#!/usr/bin/env bash\n# null shim\necho null\nexit 0\n")
    ts.chmod(0o755)
    return target


def _install_hook(repo: Path, body: str, name: str = "commit-msg") -> None:
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    h = hooks / name
    h.write_text(body)
    h.chmod(0o755)


def _refusing_hook(counter: Path) -> str:
    """Deterministic refusal, shaped like the real hot-path-size-gate."""
    return (
        "#!/bin/sh\n"
        f'echo x >> "{_to_bash_path(counter)}"\n'
        'echo "hot-path-size-gate: .claude/skills/replay/SKILL.md 67830 B exceeds '
        'the 65536 B on-demand-skills ceiling" >&2\n'
        "exit 1\n"
    )


def _flaky_transient_hook(counter: Path) -> str:
    """Fails with the LOCK signature twice, then succeeds — the transient shape
    the retry budget exists for. Emits git's own wording so the predicate under
    test sees exactly what a real collision produces."""
    c = _to_bash_path(counter)
    return (
        "#!/bin/sh\n"
        f'echo x >> "{c}"\n'
        f'n=$(wc -l < "{c}")\n'
        "if [ \"$n\" -lt 3 ]; then\n"
        '  echo "fatal: Unable to create index.lock: File exists." >&2\n'
        '  echo "Another git process seems to be running in this repository" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )


def _attempts(counter: Path) -> int:
    if not counter.exists():
        return 0
    return len([ln for ln in counter.read_text().splitlines() if ln.strip()])


def _run_commit(shim: Path, repo: Path, agent="alpha"):
    return _run_bash(
        # `--outcome deep` is REQUIRED: iteration-commit.sh skips the commit
        # entirely on routine ("skip: outcome=routine (no commit by design)"),
        # so a routine harness would exercise ZERO attempts and every assertion
        # below would pass vacuously.
        [shim, "--goal-id", "g-115-9807", "--title", "test commit",
         "--outcome", "deep", "--repo", repo],
        env={"MIND_AGENT": agent},
    )


def _signal_path(repo: Path, agent="alpha") -> Path:
    return repo / "agents" / agent / "session" / "commit-refused.json"


def _stage_work(repo: Path) -> None:
    (repo / "core" / "scripts" / "work.sh").write_text("echo work\n")
    _git(repo, "add", "-A")


# --------------------------------------------------------------------------- #
# iteration-commit.sh — the retry narrowing
# --------------------------------------------------------------------------- #

def test_deterministic_hook_refusal_makes_exactly_one_attempt():
    """THE CORE REGRESSION. A deterministic refusal must consume ONE attempt,
    not three. Pre-fix this counter read 3."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        counter = tmp / "attempts.txt"
        _install_hook(repo, _refusing_hook(counter))
        _stage_work(repo)

        r = _run_commit(shim, repo)

        assert _attempts(counter) == 1, (
            f"a deterministic refusal must be attempted ONCE, got "
            f"{_attempts(counter)} attempts. stderr:\n{r.stderr}"
        )
        assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stderr}"
        assert "REFUSED" in r.stderr
        assert "NOT retried" in r.stderr


def test_refusal_message_names_the_staged_index_consequence():
    """The refusal must SAY that the index is left staged and that pushes will
    defer — the missing link that made the 1h47m stranding unreadable."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _install_hook(repo, _refusing_hook(tmp / "attempts.txt"))
        _stage_work(repo)

        r = _run_commit(shim, repo)

        assert "INDEX IS LEFT STAGED" in r.stderr, r.stderr
        assert "guard-741" in r.stderr, r.stderr
        assert "g-115-9807" in r.stderr, r.stderr


def test_deterministic_refusal_writes_persistent_signal():
    """Outcome 2: a signal that SURVIVES to the next iteration, not one stderr
    line inside a long close."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _install_hook(repo, _refusing_hook(tmp / "attempts.txt"))
        _stage_work(repo)

        _run_commit(shim, repo)

        sig = _signal_path(repo)
        assert sig.exists(), "no persistent refusal signal was written"
        rec = json.loads(sig.read_text())
        assert rec["event"] == "commit_refused_deterministic"
        assert rec["agent"] == "alpha"
        assert str(rec["rc"]) == "1", f"measured rc for a hook refusal is 1, got {rec['rc']!r}"
        assert "hot-path-size-gate" in rec["refusal_output"], rec["refusal_output"]
        assert rec["staged_count"] >= 1, rec
        assert any("work.sh" in p for p in rec["staged_paths"]), rec["staged_paths"]


def test_index_is_left_staged_after_a_refusal():
    """EXPLICITLY OUT OF SCOPE to 'fix' — guard-741's refusal to touch a staged
    index must stay. This pins that the fix did NOT quietly unstage anything."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _install_hook(repo, _refusing_hook(tmp / "attempts.txt"))
        _stage_work(repo)

        _run_commit(shim, repo)

        staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=repo, capture_output=True, text=True, timeout=15).stdout
        assert "core/scripts/work.sh" in staged, f"staged set was altered: {staged!r}"


def test_transient_failure_still_retries_and_can_succeed():
    """POSITIVE CONTROL (guard-4166). A 'never retry' implementation passes every
    other test in this file and regresses the lock-contention recovery the loop
    exists for. The known-transient signature MUST still consume retries."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        counter = tmp / "attempts.txt"
        _install_hook(repo, _flaky_transient_hook(counter))
        _stage_work(repo)

        r = _run_commit(shim, repo)

        assert _attempts(counter) == 3, (
            f"a transient failure must still be retried, got {_attempts(counter)} "
            f"attempt(s). stderr:\n{r.stderr}"
        )
        assert r.returncode == 0, f"third attempt should have succeeded: {r.stderr}"
        assert not _signal_path(repo).exists(), "a recovered transient must write no refusal signal"


def test_successful_commit_writes_no_refusal_signal():
    """Negative control: the signal must mean something."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        _stage_work(repo)

        r = _run_commit(shim, repo)

        assert r.returncode == 0, r.stderr
        assert not _signal_path(repo).exists()


def test_a_new_unknown_gate_is_also_treated_as_deterministic():
    """GATE-AGNOSTIC BY CONSTRUCTION. A gate whose text nothing has ever seen
    must get the same one-attempt handling — this is what the inverted predicate
    buys, and the property a message-matching fix would NOT have."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        counter = tmp / "attempts.txt"
        _install_hook(repo, (
            "#!/bin/sh\n"
            f'echo x >> "{_to_bash_path(counter)}"\n'
            'echo "zzz-future-gate-nobody-has-written-yet: refused" >&2\n'
            "exit 1\n"
        ))
        _stage_work(repo)

        r = _run_commit(shim, repo)

        assert _attempts(counter) == 1, r.stderr
        assert _signal_path(repo).exists()


def test_a_pre_commit_refusal_is_also_deterministic():
    """The measured rc discriminator covers pre-commit hooks too (both exit 1)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)
        counter = tmp / "attempts.txt"
        _install_hook(repo, (
            "#!/bin/sh\n"
            f'echo x >> "{_to_bash_path(counter)}"\n'
            'echo "check-no-python-cli-fallback: regression" >&2\n'
            "exit 1\n"
        ), name="pre-commit")
        _stage_work(repo)

        r = _run_commit(shim, repo)

        assert _attempts(counter) == 1, r.stderr
        assert r.returncode == 2


# --------------------------------------------------------------------------- #
# iteration-push.sh — the streak message evidence
# --------------------------------------------------------------------------- #

def _clone_pair_local(tmp_path: Path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, timeout=15)
    a = tmp_path / "a"
    subprocess.run(["git", "clone", "-q", str(origin), str(a)], check=True, timeout=30)
    _git(a, "config", "user.email", "a@test.com")
    _git(a, "config", "user.name", "a")
    _git(a, "config", "commit.gpgsign", "false")
    (a / "agents" / "alpha").mkdir(parents=True)
    (a / "agents" / "zeta").mkdir(parents=True)
    (a / "agents" / "alpha" / "self.md").write_text("# alpha\n")
    (a / "agents" / "zeta" / "self.md").write_text("# zeta\n")
    (a / "core" / "scripts").mkdir(parents=True)
    (a / "core" / "scripts" / ".gitkeep").write_text("")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "init")
    _git(a, "push", "-q", "origin", "main")
    return origin, a


def _run_push_streak(repo: Path, agent: str):
    """Drive the REAL script with the streak alarm at 1 so one defer trips it."""
    return subprocess.run(
        [BASH, str(ITERATION_PUSH_SH), "--repo", str(repo), "--strict"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "MIND_AGENT": agent,
             "ITERATION_PUSH_DEFER_STREAK_ALARM": "1",
             "ITERATION_PUSH_MIN_COMMITS": "0",
             "ITERATION_PUSH_MIN_SECONDS": "0"},
    )


def _force_staged_defer(tmp_path: Path, staged_rel: str):
    """Make A BEHIND origin with a staged entry the incoming merge ALSO touches.

    The overlap is load-bearing and was wrong in the first draft: a staged path
    the merge does not touch merges cleanly, the self-heal never runs, and no
    streak fires — the harness then proves nothing while looking like it ran.
    Mirrors test_selfheal_staged_crossagent_defers_guard741's shape."""
    origin, a = _clone_pair_local(tmp_path)
    # seed the contested path so BOTH clones share a base for it
    p = a / staged_rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("v1\n")
    _git(a, "add", staged_rel)
    _git(a, "commit", "-qm", "seed contested path")
    _git(a, "push", "-q", "origin", "main")

    b = tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(origin), str(b)], check=True, timeout=30)
    _git(b, "config", "user.email", "b@test.com")
    _git(b, "config", "user.name", "b")
    _git(b, "config", "commit.gpgsign", "false")
    (b / staged_rel).write_text("v2-from-peer\n")
    _git(b, "add", staged_rel)
    _git(b, "commit", "-qm", "peer edits the contested path")
    _git(b, "push", "-q", "origin", "main")

    # A stages a DIVERGENT change to the same path -> merge is blocked -> the
    # self-heal sees a staged entry not identical to HEAD -> guard-741 defer.
    p.write_text("staged-locally\n")
    _git(a, "add", staged_rel)
    return a


def test_streak_evidence_reports_a_self_only_staged_set():
    """The measured case: 17 staged paths, ZERO belonging to a partner. The old
    message named only partner causes, so guard-741 'protected nothing while
    blocking everything' and the reader was sent into partner forensics."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        a = _force_staged_defer(Path(td), "agents/alpha/notes.txt")
        r = _run_push_streak(a, "alpha")
        out = r.stderr
        assert "staged-set evidence" in out, out
        assert "SELF-OWNED" in out, out
        assert "partner=0" in out, out
        assert "agents/alpha/notes.txt" in out, out


def test_streak_evidence_names_the_partner_when_one_owns_staged_work():
    """The other half of the split must still read correctly — and must keep
    saying do NOT clear, because clearing destroys a partner's unpushed work."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        a = _force_staged_defer(Path(td), "agents/zeta/state.jsonl")
        r = _run_push_streak(a, "alpha")
        out = r.stderr
        assert "staged-set evidence" in out, out
        assert "partner=1 (zeta)" in out, out
        assert "do NOT clear" in out, out


def test_streak_cause_list_names_a_refused_commit():
    """Outcome 3: the refused commit must be ON the cause list. It was the
    actual cause of the measured stranding and was not among the three named."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        a = _force_staged_defer(Path(td), "agents/alpha/notes.txt")
        r = _run_push_streak(a, "alpha")
        out = r.stderr
        assert "INTEGRATE-DEFER STREAK" in out, out
        assert "REFUSED BY A COMMIT-MSG GATE" in out, out
        assert "commit-refused.json" in out, out


def test_streak_evidence_surfaces_the_refusal_signal_when_present():
    """End-to-end link between the two scripts: the signal iteration-commit
    leaves behind is what turns the streak message from a guess into a pointer."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        a = _force_staged_defer(Path(td), "agents/alpha/notes.txt")
        sess = a / "agents" / "alpha" / "session"
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "commit-refused.json").write_text(
            json.dumps({"event": "commit_refused_deterministic", "rc": "1"})
        )
        r = _run_push_streak(a, "alpha")
        assert "REFUSED-COMMIT SIGNAL PRESENT" in r.stderr, r.stderr
