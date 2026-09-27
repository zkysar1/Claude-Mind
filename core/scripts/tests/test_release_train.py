"""test_release_train.py — _release_train.py + release-train-check.py ().

The time-push trigger from promotion-runbook.md "Who cuts, and WHEN": the
newest v* tag reachable from origin/main is >= stale_hours old AND framework
commits sit past it. These tests pin the measurement against REAL git repos
(a bare origin + a clone in tmp_path — guard-1094: nothing here touches a
production queue, board or ref), then the CLI's three modes:

  1. Config comes from aspirations.yaml (guard-308), and the signal survives
     the real origin_signal gate unrewritten (guard-2329).
  2. Only non-merge commits touching the copy set count — agent-state churn
     and merge commits do not (the guard-5202 always-firing failure mode).
  3. "Newest" is by VERSION among tags merged into origin/main.
  4. The verdict needs BOTH halves; the threshold is inclusive.
  5. Open-goal lookup matches the PREFIX and open statuses only.
  6. CLI: non-frontier is silent in --nudge; --nudge speaks only when due
     AND an open goal exists for the newest tag; exit codes 0/2.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _release_train as rt  # noqa: E402

PATHS = ["core/scripts", ".claude/skills"]
OLD = "2020-01-01T00:00:00Z"       # a tag date far past any stale_hours
OLD_TS = 1577836800.0


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


class Repo:
    """A clone with a bare `origin`, hooks disabled, signing off."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        _git(tmp, "init", "--bare", str(self.origin))
        _git(tmp, "init", str(self.work))
        for k, v in (("user.email", "t@example.invalid"), ("user.name", "t"),
                     ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
                     ("core.hooksPath", str(tmp / "no-hooks")), ("core.autocrlf", "false")):
            _git(self.work, "config", k, v)
        _git(self.work, "checkout", "-b", "main")
        _git(self.work, "remote", "add", "origin", str(self.origin))

    def commit(self, rel: str, msg: str) -> None:
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{msg}\n{time.time_ns()}\n", encoding="utf-8")
        _git(self.work, "add", rel)
        _git(self.work, "commit", "-m", msg)

    def tag(self, name: str, date: str = OLD) -> None:
        _git(self.work, "tag", "-a", name, "-m", name, date=date)

    def push(self) -> None:
        _git(self.work, "push", "--quiet", "origin", "main", "--follow-tags")
        _git(self.work, "fetch", "--quiet", "origin")


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path)
    r.commit("core/scripts/a.py", "base")
    r.tag("v1.0.0")
    r.push()
    return r


def _world(tmp_path: Path, role: str | None = "frontier", goals: list | None = None) -> Path:
    w = tmp_path / "world"
    (w / "config").mkdir(parents=True, exist_ok=True)
    if role is not None:
        (w / "config" / "compatibility.yaml").write_text(f"self_role: {role}\n", encoding="utf-8")
    if goals is not None:
        rec = {"id": "asp-900", "title": "t", "status": "active", "goals": goals}
        (w / "aspirations.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return w


# ── 1. config + signal ───────────────────────────────────────────────────────

def test_config_values_come_from_aspirations_yaml():
    import yaml
    with (CORE_SCRIPTS.parent / "config" / "aspirations.yaml").open(encoding="utf-8") as f:
        declared = (yaml.safe_load(f) or {}).get("release_train") or {}
    assert declared, "aspirations.yaml must declare a release_train block"
    got = rt.config()
    assert set(got) == {"stale_hours", "ticks_to_file", "ticks_to_revalidate"}
    for key, value in declared.items():
        assert got[key] == value, f"{key}: read {got[key]}, config declares {value}"


def test_config_floor_on_missing_file(tmp_path):
    assert rt.config(tmp_path / "absent.yaml") == rt._CONFIG_FLOOR


def test_signal_survives_the_real_origin_signal_gate():
    """guard-2329: an unsanctioned prefix is silently REWRITTEN, which would
    make the probe's dedup vacuous forever. Pin that ours is accepted as-is."""
    from gates import origin_signal as og
    sig = rt.signal_for("v2.12.84")
    assert sig == "investigate:release-train-stalled-past-v2.12.84"
    assert og.is_valid(sig)
    res = og.evaluate({"title": "Investigate: release train stalled - v2.12.84 ...",
                       "origin_signal": sig, "source": "world"}, agent_name="t")
    assert res.get("would_block") is not True
    assert res.get("origin_signal", sig) == sig


def test_framework_paths_is_the_preflight_copy_set():
    paths = rt.framework_paths()
    assert "core/scripts" in paths and ".claude/skills" in paths and "mind_api/src" in paths
    assert "agents" not in paths


# ── self_role ────────────────────────────────────────────────────────────────

def test_self_role_reads_the_world_overlay(tmp_path):
    assert rt.self_role(_world(tmp_path, "frontier")) == "frontier"


def test_self_role_is_none_when_missing_or_malformed(tmp_path):
    assert rt.self_role(None) is None
    assert rt.self_role(_world(tmp_path, role=None)) is None
    (tmp_path / "world" / "config" / "compatibility.yaml").write_text(
        "self_role: [not, a, string]\n", encoding="utf-8")
    assert rt.self_role(tmp_path / "world") is None
    (tmp_path / "world" / "config" / "compatibility.yaml").write_text(
        ":\n  - broken: [", encoding="utf-8")
    assert rt.self_role(tmp_path / "world") is None


# ── 2-3. measure ─────────────────────────────────────────────────────────────

def test_measure_counts_only_framework_non_merge_commits(repo):
    repo.commit("core/scripts/b.py", "framework change")
    repo.commit("agents/alpha/journal.jsonl", "agent-state churn")
    # A merge commit whose side branch touches the copy set: the side commit
    # counts once, the merge itself not at all.
    _git(repo.work, "checkout", "-b", "side")
    repo.commit(".claude/skills/x/SKILL.md", "side framework change")
    _git(repo.work, "checkout", "main")
    repo.commit("agents/alpha/notes.md", "more churn")
    _git(repo.work, "merge", "--no-ff", "-m", "merge side", "side")
    repo.push()
    m = rt.measure(repo.work, PATHS, now=OLD_TS + 3600 * 30)
    assert m["error"] is None
    assert m["newest_tag"] == "v1.0.0"
    assert m["commits_past"] == 2
    assert len(m["commit_sample"]) == 2
    assert all("churn" not in s and "merge side" not in s for s in m["commit_sample"])


def test_measure_age_is_the_tag_creation_date(repo):
    m = rt.measure(repo.work, PATHS, now=OLD_TS + 3600 * 30)
    assert m["tag_created"] == OLD
    assert m["tag_age_hours"] == 30.0
    assert m["commits_past"] == 0


def test_newest_is_by_version_and_must_be_merged_into_origin_main(repo):
    repo.commit("core/scripts/c.py", "c")
    repo.tag("v1.9.0")
    repo.commit("core/scripts/d.py", "d")
    repo.tag("v1.10.0")
    repo.push()
    # A higher tag that origin/main does not contain must not count.
    _git(repo.work, "checkout", "-b", "unmerged")
    repo.commit("core/scripts/e.py", "e")
    repo.tag("v9.0.0")
    _git(repo.work, "checkout", "main")
    m = rt.measure(repo.work, PATHS, now=OLD_TS)
    assert m["newest_tag"] == "v1.10.0"
    assert m["commits_past"] == 0


def test_measure_reports_errors_instead_of_raising(tmp_path):
    r = Repo(tmp_path)
    r.commit("core/scripts/a.py", "untagged")
    no_origin = rt.measure(r.work, PATHS)
    assert no_origin["error"] and no_origin["newest_tag"] is None
    r.push()
    no_tag = rt.measure(r.work, PATHS)
    assert no_tag["error"] == "no v* tag is reachable from origin/main"
    assert rt.measure(tmp_path / "not-a-repo", PATHS)["error"]


# ── 4. decide ────────────────────────────────────────────────────────────────

def _m(age, n, error=None):
    return {"newest_tag": "v1.0.0", "tag_age_hours": age, "commits_past": n, "error": error}


def test_decide_needs_both_halves_and_is_inclusive():
    assert rt.decide(_m(24.0, 1), 24)["due"] is True
    assert rt.decide(_m(23.9, 50), 24)["due"] is False
    assert rt.decide(_m(900.0, 0), 24)["due"] is False
    assert "no framework commits" in rt.decide(_m(900.0, 0), 24)["reason"]
    unmeasured = rt.decide(_m(None, None, error="boom"), 24)
    assert unmeasured == {"due": False, "reason": "unmeasured: boom"}


# ── 5. open goals ────────────────────────────────────────────────────────────

def test_open_release_goals_matches_prefix_and_open_status_only(tmp_path):
    sig = rt.signal_for("v1.0.0")
    world = _world(tmp_path, goals=[
        {"id": "g-1", "status": "pending", "origin_signal": sig},
        {"id": "g-2", "status": "completed", "origin_signal": sig},
        {"id": "g-3", "status": "pending", "origin_signal": "investigate:git-drift-detected-x"},
        {"id": "g-4", "status": "in-progress", "origin_signal": rt.signal_for("v0.9.0")},
    ])
    got = rt.open_release_goals(world)
    assert sorted(g["id"] for g in got) == ["g-1", "g-4"]
    assert all(g["_source"] == "world" for g in got)
    assert rt.open_release_goals(tmp_path / "absent") == []


# ── 6. CLI ───────────────────────────────────────────────────────────────────

CLI = CORE_SCRIPTS / "release-train-check.py"


def _cli(repo_dir: Path, world: Path, *flags: str):
    proc = subprocess.run([sys.executable, str(CLI), "--repo", str(repo_dir),
                           "--world-dir", str(world), *flags],
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout.strip(), proc.stderr


def test_cli_non_frontier_is_quiet(repo, tmp_path):
    world = _world(tmp_path, role="downstream")
    rc, out, _ = _cli(repo.work, world)
    assert rc == 0 and "not this deployment's train" in out
    rc, out, _ = _cli(repo.work, world, "--nudge")
    assert rc == 0 and out == ""


def test_cli_due_exit_code_and_nudge_needs_an_open_goal(repo, tmp_path):
    repo.commit("core/scripts/late.py", "framework change past the old tag")
    repo.push()
    world = _world(tmp_path, goals=[])
    rc, out, _ = _cli(repo.work, world)
    assert rc == 2 and out.startswith("release-train: DUE")
    rc, out, _ = _cli(repo.work, world, "--json")
    data = json.loads(out)
    assert rc == 2 and data["verdict"]["due"] is True
    assert data["signal"] == rt.signal_for("v1.0.0")
    # Due but no open goal: the nudge has nothing to name, so it is silent.
    rc, out, _ = _cli(repo.work, world, "--nudge")
    assert rc == 0 and out == ""
    world = _world(tmp_path, goals=[
        {"id": "g-115-77777", "status": "pending", "origin_signal": rt.signal_for("v1.0.0")}])
    rc, out, _ = _cli(repo.work, world, "--nudge")
    assert rc == 0
    assert out.startswith("[release-train] LLM-ACTION: release train stalled")
    assert "g-115-77777" in out and "promotion-runbook.md" in out


def test_cli_not_due_when_tag_is_fresh(tmp_path):
    r = Repo(tmp_path)
    r.commit("core/scripts/a.py", "base")
    r.tag("v2.0.0", date=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    r.commit("core/scripts/b.py", "past a fresh tag")
    r.push()
    world = _world(tmp_path, goals=[
        {"id": "g-1", "status": "pending", "origin_signal": rt.signal_for("v2.0.0")}])
    rc, out, _ = _cli(r.work, world)
    assert rc == 0 and out.startswith("release-train: ok")
    rc, out, _ = _cli(r.work, world, "--nudge")
    assert out == ""
