"""test_pending_deploys.py — 8-a (pending-deploys hard gate, CAPTURE).

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
        # TRAILING token (no following space): the 8-a fresh-eyes
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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
