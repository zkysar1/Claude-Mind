"""test_pending_deploys.py — -a (pending-deploys hard gate, CAPTURE).

Verifies the two SG-a artifacts:
  - core/scripts/pending-deploys.py — session-local tracker (add/dedup/list/
    has-pending/clear/resolve), fail-open.
  - core/scripts/deploy-detect-hook.sh — PostToolUse[Bash] hook that registers a
    deploy-verification obligation on a real `git push` and NOTHING else.

The hook tests mirror test_bash_edit_record.py: a temp repo whose core/scripts
holds the real hook + pending-deploys.py + _paths.sh + a .python-shim (execing
sys.executable, NOT `py -3` — the g-115-1836 mutual-recursion trap), plus a
SEPARATE temp product git repo (with a commit + origin remote) to push from.

Detection BIASES TOWARD PRECISION: a false-positive obligation for an unpushed
sha can never be resolved by deploy-verify (no CI runs -> unverified forever),
so `git stash push`, `git push --dry-run`, `echo push`, and non-push commands
must all register NOTHING.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TRACKER = CORE_SCRIPTS / "pending-deploys.py"
HOOK = CORE_SCRIPTS / "deploy-detect-hook.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_pending_deploys_test"

_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
    "BODY_",  # : BODY_WM_PATH is the FIRST branch of wm_path()
)

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _hermetic_env(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"}
    env["STORAGE_BACKEND"] = "local"
    env.update(overrides)
    return env


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# ── Tracker tests (pure, via --store) ──────────────────────────────────────

def _tracker(store, *args, env=None):
    e = dict(os.environ)
    e["STORAGE_BACKEND"] = "local"
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(TRACKER), "--store", str(store), *args],
                          capture_output=True, text=True, timeout=30, env=e)


def test_tracker_add_list_dedup():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "a" * 40
        _tracker(store, "add", "--repo", "o/r1", "--sha", sha, "--goal-id", "g-1", "--dir", "/d1")
        _tracker(store, "add", "--repo", "o/r1", "--sha", sha, "--goal-id", "g-1", "--dir", "/d1")  # dup
        _tracker(store, "add", "--repo", "o/r2", "--sha", "b" * 40, "--goal-id", "g-2", "--dir", "/d2")
        out = json.loads(_tracker(store, "list", "--json").stdout)
        assert len(out) == 2, f"dedup failed: {out}"
        assert {e["repo"] for e in out} == {"o/r1", "o/r2"}


def test_tracker_list_by_goal_and_has_pending():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        _tracker(store, "add", "--repo", "o/r1", "--sha", "a" * 40, "--goal-id", "g-1", "--dir", "/d")
        _tracker(store, "add", "--repo", "o/r2", "--sha", "b" * 40, "--goal-id", "g-2", "--dir", "/d")
        one = json.loads(_tracker(store, "list", "--goal-id", "g-1", "--json").stdout)
        assert len(one) == 1 and one[0]["goal_id"] == "g-1"
        assert _tracker(store, "has-pending").returncode == 0            # any pending
        assert _tracker(store, "has-pending", "--goal-id", "g-1").returncode == 0
        assert _tracker(store, "has-pending", "--goal-id", "g-nope").returncode == 1  # none


def test_tracker_clear():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "a" * 40
        _tracker(store, "add", "--repo", "o/r1", "--sha", sha, "--goal-id", "g-1", "--dir", "/d")
        _tracker(store, "add", "--repo", "o/r2", "--sha", "b" * 40, "--goal-id", "g-2", "--dir", "/d")
        res = json.loads(_tracker(store, "clear", "--repo", "o/r1", "--sha", sha).stdout)
        assert res["cleared"] == 1 and res["remaining"] == 1
        out = json.loads(_tracker(store, "list", "--json").stdout)
        assert [e["repo"] for e in out] == ["o/r2"]


def test_tracker_fail_open_no_agent():
    """No --store and no agent -> add is a silent no-op (exit 0), never raises."""
    env = {k: v for k, v in os.environ.items() if k != "MIND_AGENT"}
    env["STORAGE_BACKEND"] = "local"
    r = subprocess.run([sys.executable, str(TRACKER), "add", "--repo", "o/r", "--sha", "a" * 40],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0


def test_tracker_missing_store_lists_empty():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "does-not-exist.yaml"
        assert json.loads(_tracker(store, "list", "--json").stdout) == []
        assert _tracker(store, "has-pending").returncode == 1


# ── Hook tests (subprocess, temp mind repo + temp product git repo) ─────────

def _setup_mind_repo(tmp: Path, agent="zeta") -> Path:
    repo = tmp / "repo"
    (repo / "agents" / agent / "session").mkdir(parents=True)
    (repo / "agents" / agent / "self.md").write_text(f"# {agent}\n")
    (repo / "agents" / agent / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (repo / ".claude").mkdir()
    for fname in ("deploy-detect-hook.sh", "pending-deploys.py", "_paths.sh"):
        dst = core_scripts / fname
        dst.write_bytes((CORE_SCRIPTS / fname).read_bytes())
        dst.chmod(0o755)
    shim_dir = core_scripts / ".python-shim"
    shim_dir.mkdir()
    for name in ("python3", "python"):
        s = shim_dir / name
        s.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
        s.chmod(0o755)
    return repo


def _setup_product_repo(tmp: Path, remote="https://github.com/owner/prod.git") -> Path:
    prod = tmp / "prod"
    prod.mkdir()
    env = _hermetic_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "remote", "add", "origin", remote]):
        subprocess.run(cmd, cwd=prod, env=env, capture_output=True, timeout=30)
    (prod / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=prod, env=env, capture_output=True, timeout=30)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=prod, env=env, capture_output=True, timeout=30)
    return prod


def _seed_diary(repo: Path, agent: str, goal_id: str):
    diary = repo / "agents" / agent / "session" / "execution-diary.jsonl"
    diary.write_text(json.dumps({"entry_type": "phase_start", "phase": "phase-4-execute",
                                 "timestamp": "2026-07-19T14:00:00", "goal_id": goal_id}) + "\n")


def _run_hook(repo: Path, payload: str, **env_overrides):
    return subprocess.run([GIT_BASH, _to_bash_path(repo / "core" / "scripts" / "deploy-detect-hook.sh")],
                          input=payload, capture_output=True, text=True, timeout=30,
                          env=_hermetic_env(**env_overrides))


def _pd_store(repo: Path, agent="zeta") -> Path:
    return repo / "agents" / agent / "session" / "pending-deploys.yaml"


def _payload(command: str, prod: Path, sid="") -> str:
    return json.dumps({"session_id": sid, "tool_input": {"command": command},
                       "cwd": _to_bash_path(prod)})


def _prod_head(prod: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=prod, capture_output=True,
                          text=True, timeout=30).stdout.strip()


def test_hook_registers_on_git_push():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        prod = _setup_product_repo(Path(td))
        _seed_diary(repo, "zeta", "g-115-2688-a")
        r = _run_hook(repo, _payload(f"git -C {_to_bash_path(prod)} push origin main", prod),
                      MIND_AGENT="zeta")
        assert r.returncode == 0, f"crashed: {r.stderr!r}"
        store = _pd_store(repo)
        assert store.exists(), f"no obligation registered; stderr={r.stderr!r}"
        import yaml
        entries = yaml.safe_load(store.read_text())
        assert len(entries) == 1
        e = entries[0]
        assert e["repo"] == "owner/prod", f"repo parse wrong: {e}"
        assert e["sha"] == _prod_head(prod), f"sha not captured at push time: {e}"
        assert e["goal_id"] == "g-115-2688-a", f"goal_id not from diary: {e}"


def test_hook_ignores_git_stash_push():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        prod = _setup_product_repo(Path(td))
        r = _run_hook(repo, _payload(f"git -C {_to_bash_path(prod)} stash push", prod),
                      MIND_AGENT="zeta")
        assert r.returncode == 0
        assert not _pd_store(repo).exists(), "git stash push wrongly registered a deploy obligation"


def test_hook_ignores_dry_run():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        prod = _setup_product_repo(Path(td))
        pd = _to_bash_path(prod)
        # Every dry-run form must register NOTHING — including short `-n` as a
        # TRAILING token (no following space): the -a fresh-eyes
        # finding was that `*" -n "*` alone missed `git push -n` /
        # `git push origin main -n`, registering a false-positive obligation.
        for cmd in (f"git -C {pd} push --dry-run",
                    f"git -C {pd} push -n",
                    f"git -C {pd} push origin main -n"):
            r = _run_hook(repo, _payload(cmd, prod), MIND_AGENT="zeta")
            assert r.returncode == 0
            assert not _pd_store(repo).exists(), f"dry-run form wrongly registered: {cmd}"


def test_hook_ignores_non_push_and_echo_push():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        prod = _setup_product_repo(Path(td))
        for cmd in ("ls -la", 'echo "remember to push"', f"git -C {_to_bash_path(prod)} log"):
            r = _run_hook(repo, _payload(cmd, prod), MIND_AGENT="zeta")
            assert r.returncode == 0
        assert not _pd_store(repo).exists(), "a non-push command wrongly registered"


def test_hook_skips_repo_without_origin_remote():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        # product repo with NO origin remote
        prod = Path(td) / "noremote"
        prod.mkdir()
        env = _hermetic_env()
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=prod, env=env, capture_output=True, timeout=30)
        (prod / "f").write_text("x")
        subprocess.run(["git", "add", "."], cwd=prod, env=env, capture_output=True, timeout=30)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=prod, env=env, capture_output=True, timeout=30)
        r = _run_hook(repo, _payload(f"git -C {_to_bash_path(prod)} push", prod), MIND_AGENT="zeta")
        assert r.returncode == 0
        assert not _pd_store(repo).exists(), "push in a repo with no origin remote wrongly registered"


def test_hook_resolves_agent_from_session_binding():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        prod = _setup_product_repo(Path(td))
        sid = "sess-xyz-9"
        bdir = repo / "agents" / "zeta" / "sessions" / sid
        bdir.mkdir(parents=True)
        (bdir / "binding.yaml").write_text("agent: zeta\nmode: autonomous\n")
        _seed_diary(repo, "zeta", "g-115-2688-a")
        # No MIND_AGENT -> must resolve from binding.
        r = _run_hook(repo, _payload(f"git -C {_to_bash_path(prod)} push", prod, sid=sid))
        assert r.returncode == 0, f"crashed: {r.stderr!r}"
        assert _pd_store(repo).exists(), "binding-resolved obligation missing"


def test_hook_fail_open_empty_stdin():
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_mind_repo(Path(td))
        assert _run_hook(repo, "").returncode == 0
        assert _run_hook(repo, '{"tool_input":{"command":"ls"}}').returncode == 0


# ── Defect 1: resolve landed-detection () ─────────────────────────
# cmd_resolve tests run IN-PROCESS (import the module, fake subprocess.run) so
# BOTH the deploy-verify hop AND the gh landed-detection probes are driven
# deterministically without a network, a real repo, or a fake-gh-on-PATH. The
# gate test (test_pending_deploys_gate.py) covers the end-to-end subprocess
# wiring + store-clearing through the SG-b gate.

import contextlib  # noqa: E402
import io  # noqa: E402
import importlib.util  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402


def _load_pd():
    spec = importlib.util.spec_from_file_location("pending_deploys_mod", TRACKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PD = _load_pd()


class _FakeProc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_run(deploy_rc, deploy_status, *, default_branch="main",
              ahead_by="5", merged="0", sha_present=True, gh_calls=None,
              flaky_pc=False):
    """subprocess.run stand-in: routes deploy-verify.sh -> (rc, status JSON) and
    `gh api ...` -> the landed-detection + rebase-orphan probe values (default
    branch / ahead_by / merged-PR count / commit-exists). default_branch=None
    simulates an unusable gh; sha_present=False simulates a REBASE ORPHAN the
    remote never saw (bare repos/<repo>/commits/<sha> -> 404/422). flaky_pc
    simulates a TRANSIENT gh error (rate-limit/timeout) on ONLY the re-confirm
    default-branch read: the 1st bare repos/<repo> read succeeds, the 2nd
    fails (Finding 1 fail-safe boundary)."""
    pc = {"n": 0}   # counts bare repos/<repo> default-branch reads: the positive
                    # control + the post-Finding-1 re-confirm. Drives flaky_pc.
    def run(cmd, **kw):
        joined = " ".join(str(c) for c in cmd)
        if "deploy-verify.sh" in joined:
            return _FakeProc(deploy_rc, json.dumps({"status": deploy_status}))
        if cmd and str(cmd[0]) == "gh":
            if gh_calls is not None:
                gh_calls.append(joined)
            api = str(cmd[2]) if len(cmd) > 2 else ""
            if "/compare/" in api:
                return _FakeProc(0, ahead_by)
            if "/pulls" in api:                  # repos/<repo>/commits/<sha>/pulls
                return _FakeProc(0, merged)
            if "/commits/" in api:               # bare commit lookup (rebase-orphan probe)
                return _FakeProc(0, "f" * 40) if sha_present else _FakeProc(1, "")
            # bare repos/<repo> -q .default_branch: the positive control AND (post
            # Finding 1) the re-confirm read. flaky_pc fails ONLY the 2nd read.
            pc["n"] += 1
            if default_branch is None or (flaky_pc and pc["n"] >= 2):
                return _FakeProc(1, "")          # gh error -> positive control fails
            return _FakeProc(0, default_branch)  # repos/<repo> -q .default_branch
        return _FakeProc(0, "")
    return run


def _resolve_in_process(store, repo, sha, runner):
    args = SimpleNamespace(repo=repo, sha=sha, dir="", timeout_mins=None,
                           subprocess_timeout=None, store=str(store), agent=None)
    buf = io.StringIO()
    with patch.object(_PD.subprocess, "run", runner), contextlib.redirect_stdout(buf):
        rc = _PD.cmd_resolve(args)
    line = [l for l in buf.getvalue().splitlines() if l.strip().startswith("{")][-1]
    return rc, json.loads(line)


def _seed_store(store, repo, sha):
    _tracker(store, "add", "--repo", repo, "--sha", sha, "--goal-id", "g-1")


def _store_shas(store):
    return {e["sha"] for e in json.loads(_tracker(store, "list", "--json").stdout)}


def test_resolve_clears_on_ancestor_landed():
    """deploy-verify FAILED, but the sha is an ancestor of the default branch
    (ahead_by==0) -> landed-detection clears the entry (rc 0, landed_via)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "a" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(1, "failed", ahead_by="0"))
        assert rc == 0, out
        assert out["cleared"] is True and out["landed_via"].startswith("ancestor:"), out
        assert sha not in _store_shas(store), "ancestor-landed entry not cleared"


def test_resolve_clears_on_merged_pr():
    """deploy-verify UNVERIFIED, sha NOT an ancestor, but its PR is merged ->
    landed-detection clears the entry (rc 0, landed_via=merged-pr)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "b" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(2, "unverified", ahead_by="7", merged="1"))
        assert rc == 0, out
        assert out["cleared"] is True and out["landed_via"] == "merged-pr", out
        assert sha not in _store_shas(store), "merged-pr entry not cleared"


def test_resolve_keeps_when_not_landed():
    """deploy-verify FAILED and the sha neither is an ancestor nor has a merged
    PR -> entry KEPT, rc mirrors deploy-verify (1)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "c" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(1, "failed", ahead_by="4", merged="0"))
        assert rc == 1, out
        assert out["cleared"] is False, out
        assert sha in _store_shas(store), "genuinely-failed entry must be kept"


def test_resolve_keeps_when_gh_unusable():
    """FAIL-SAFE: deploy-verify FAILED and gh cannot read the default branch
    (positive control fails) -> entry KEPT, never a spurious clear (rb-3434)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "d" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(1, "failed", default_branch=None))
        assert rc == 1, out
        assert out["cleared"] is False, out
        assert sha in _store_shas(store), "gh-unusable must NOT clear (fail-safe)"


def test_resolve_ok_path_skips_landed_check():
    """Regression: an ok CI verdict clears WITHOUT invoking landed-detection
    (no gh call) -- landed-detection is only for the failed/unverified path."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "e" * 40
        _seed_store(store, "o/r", sha)
        gh_calls = []
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(0, "ok", gh_calls=gh_calls))
        assert rc == 0 and out["cleared"] is True, out
        assert "landed_via" not in out, "ok path must not report landed_via"
        assert gh_calls == [], "landed-detection must not run on the ok path"
        assert sha not in _store_shas(store)


# ──  / rb-4737: rebase-orphan retirement + non-deploying-branch skip ──

def test_resolve_retires_rebased_away():
    """deploy-verify UNVERIFIED, sha did NOT land (not ancestor, no merged PR),
    AND the sha is ABSENT from origin (rebase orphan: superseded by git pull
    --rebase, never pushed) -> retire the phantom entry (rc 0, retired_via)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "a" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(2, "unverified", ahead_by="7",
                                                merged="0", sha_present=False))
        assert rc == 0, out
        assert out["cleared"] is True and out.get("retired_via") == "rebased-away", out
        assert sha not in _store_shas(store), "rebased-away phantom not retired"


def test_resolve_keeps_present_sha_not_retired():
    """Boundary: a sha that IS on origin (present) but deploy FAILED and did not
    land must be KEPT — never mis-retired as rebased-away (the absent-check must
    not over-retire a genuinely-failing deploy)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "c" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(1, "failed", ahead_by="4",
                                                merged="0", sha_present=True))
        assert rc == 1, out
        assert out["cleared"] is False and "retired_via" not in out, out
        assert sha in _store_shas(store), "present-but-failed sha must be kept"


def test_resolve_rebased_away_gated_by_positive_control():
    """FAIL-SAFE: if gh cannot read the default branch (positive control fails),
    the absent-check must NOT retire even a would-be rebase-orphan — an unusable
    gh is not evidence of absence (rb-4740). Entry kept, rc mirrors deploy."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "d" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(2, "unverified", default_branch=None,
                                                sha_present=False))
        assert rc == 2, out
        assert out["cleared"] is False, out
        assert sha in _store_shas(store), "gh-unusable must never retire (fail-safe)"


def test_resolve_keeps_on_flaky_positive_control_reconfirm():
    """Finding 1 (fresh-eyes ): the commit-lookup and the positive
    control are SEPARATE gh calls; _gh collapses every non-zero exit to None, so
    a TRANSIENT error (rate-limit 403 / timeout) on ONLY the commit-lookup would
    — without the re-confirm — be misread as genuine absence and RETIRE a real
    obligation. Here the 1st default-branch read (positive control) succeeds, the
    commit-lookup returns not-found, and the RE-CONFIRM default-branch read fails
    (transient). The entry MUST be kept (fail-safe), never retired."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        sha = "e" * 40
        _seed_store(store, "o/r", sha)
        rc, out = _resolve_in_process(store, "o/r", sha,
                                      _fake_run(2, "unverified", ahead_by="7",
                                                merged="0", sha_present=False,
                                                flaky_pc=True))
        assert rc == 2, out
        assert out["cleared"] is False and "retired_via" not in out, out
        assert sha in _store_shas(store), \
            "transient gh error on re-confirm must never retire (Finding 1 fail-safe)"


def _setup_branch_repo(td) -> Path:
    """A real git repo whose current branch is `main` and whose origin/HEAD is
    set locally (no network) to origin/main, so _push_branch_deploys can resolve
    current-vs-default without a remote."""
    repo = Path(td) / "branchrepo"
    repo.mkdir()
    env = _hermetic_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], env=env,
                       capture_output=True, text=True, timeout=30)

    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    g("add", ".")
    g("commit", "-q", "-m", "init")
    g("branch", "-M", "main")  # name the branch `main` regardless of git's default
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          env=env, capture_output=True, text=True, timeout=30).stdout.strip()
    # Simulate origin/main + origin/HEAD LOCALLY (no network fetch needed).
    g("update-ref", "refs/remotes/origin/main", head)
    g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return repo


def test_add_registers_on_default_branch():
    """A commit on the repo's default branch (main) DOES register — the deploy
    workflow triggers there, so the obligation is real."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup_branch_repo(td)
        store = Path(td) / "pd.yaml"
        _tracker(store, "add", "--repo", "o/r", "--sha", "a" * 40,
                 "--goal-id", "g-1", "--dir", str(repo))
        assert _tracker(store, "has-pending").returncode == 0, \
            "default-branch push must register a deploy obligation"


def test_add_skips_non_default_branch():
    """A commit on a docs/side branch (not the default) does NOT trigger the
    deploy workflow, so it must be SKIPPED at registration (g-115-2925 / rb-4737,
    the 50fc8d1 docs/operator-hardening phantom class)."""
    with tempfile.TemporaryDirectory() as td:
        repo = _setup_branch_repo(td)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "docs/x"],
                       env=_hermetic_env(), capture_output=True, text=True, timeout=30)
        store = Path(td) / "pd.yaml"
        _tracker(store, "add", "--repo", "o/r", "--sha", "b" * 40,
                 "--goal-id", "g-2", "--dir", str(repo))
        assert _tracker(store, "has-pending").returncode == 1, \
            "non-default-branch push must be skipped (no deploy obligation)"


def test_add_fail_open_non_git_dir():
    """FAIL-OPEN: a --dir that is not a git repo (branch unresolvable) must still
    register — the branch skip only fires on a CONFIRMED non-default branch."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        _tracker(store, "add", "--repo", "o/r", "--sha", "a" * 40,
                 "--goal-id", "g-1", "--dir", str(Path(td) / "not-a-repo"))
        assert _tracker(store, "has-pending").returncode == 0, \
            "non-git --dir must fail-open (register), never silently skip"


# ── : an owner-less repo is unresolvable BY CONSTRUCTION ──────────
#
# guard-4166: this fix's effect is an ABSENCE — a malformed row stops being
# recorded — and an absence assertion is satisfied by the DO-NOTHING world. A
# cmd_add that recorded nothing at all would also pass "the bad row is not in
# the store". So the refusal assertions below are deliberately PAIRED with a
# positive-existence control in the SAME test: the qualified row must STILL be
# recorded. Under a mutant that reverts the guard, the refusal assertions go
# RED while the control stays GREEN — that asymmetry is the evidence, not the
# redness. Do NOT "simplify" the control away; without it both halves would be
# green against a completely dead cmd_add.

def test_tracker_add_still_records_qualified_repo():
    """POSITIVE CONTROL for the two refusal pins below (guard-4166).

    It lives in its OWN test on purpose. Folded into the refusal test it would
    be unobservable: the refusal assertion fires first, so the test would go
    red under the mutant before this half ever executed, and 'is the control
    still green?' — the actual evidence — could not be answered. Separate, the
    mutation run reads directly: this GREEN, the two refusal pins RED.
    """
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        ok = _tracker(store, "add", "--repo", "zkysar1/Ayoai-Operator", "--sha", "b" * 40,
                      "--goal-id", "g-1", "--dir", "/d")
        assert ok.returncode == 0, f"qualified repo must still record: {ok.stderr}"
        rows = json.loads(_tracker(store, "list", "--json").stdout)
        assert [r["repo"] for r in rows] == ["zkysar1/Ayoai-Operator"], rows


def test_tracker_add_refuses_owner_less_repo():
    """`gh api repos/Ayoai-Operator` is a 404 on every box and in every session,
    so recording that row manufactures an obligation that can never clear —
    measured: two such rows sat unresolvable for 19 days (g-335-1313)."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        bad = _tracker(store, "add", "--repo", "Ayoai-Operator", "--sha", "1aa65b9",
                       "--goal-id", "g-335-809", "--dir", "")
        assert bad.returncode == 2, f"expected refusal rc=2, got {bad.returncode}"
        assert "REFUSED" in bad.stderr, bad.stderr
        rows = json.loads(_tracker(store, "list", "--json").stdout)
        assert rows == [], f"malformed row must not be stored: {rows}"


def test_tracker_resolve_reports_malformed_repo_as_usage_error():
    """rc 3 ('usage error (kept)') separates an unresolvable entry from a
    transient gh failure. Both previously printed rc=2 through the gate's
    catch-all else-branch, so the log line could not tell a reader whether to
    inspect the ENTRY or re-check gh auth — and two agents chose gh, twice."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "pd.yaml"
        r = _tracker(store, "resolve", "--repo", "Ayoai-Operator", "--sha", "1aa65b9")
        assert r.returncode == 3, f"expected rc=3, got {r.returncode}: {r.stdout}{r.stderr}"
        out = json.loads(r.stdout)
        assert out["status"] == "malformed-repo", out
        assert out["cleared"] is False, out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
