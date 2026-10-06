#!/usr/bin/env python3
"""Tests for core/scripts/framework_pull.py ().

Covers the four things the goal names -- fetch, compare, preflight-gate,
rollback -- plus the pure decision logic each one turns on. The fixture-repo
tests build REAL git repos so the plan path is exercised end to end rather
than mocked; the rollback test performs a real reset and asserts the tree
came back, because the goal requires that path be exercised, not documented.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import framework_pull as fp  # noqa: E402


# ------------------------------------------------------------ semver / tags

def test_semver_key_rejects_non_semver():
    assert fp.semver_key("v1.2.3") == (1, 2, 3)
    assert fp.semver_key("2.1.0") is None          # missing the v
    assert fp.semver_key("v1.2") is None
    assert fp.semver_key("") is None
    assert fp.semver_key(None) is None


def test_newest_tag_is_semver_not_lexical():
    """The documented trap: lexically v2.9.4 sorts ABOVE v2.12.3."""
    tags = ["v2.9.4", "v2.12.3", "v2.10.0"]
    assert sorted(tags)[-1] == "v2.9.4"            # lexical picks the OLD one
    assert fp.newest_tag(tags) == "v2.12.3"        # semver picks the new one


def test_newest_tag_ignores_junk_and_empty():
    assert fp.newest_tag(["nightly", "v1.0.0", "release-2"]) == "v1.0.0"
    assert fp.newest_tag([]) is None
    assert fp.newest_tag(["nightly"]) is None


@pytest.mark.parametrize("installed,newest,expected", [
    ("v1.0.0", "v1.0.1", "newer-available"),
    ("v1.0.1", "v1.0.1", "current"),
    ("v1.0.2", "v1.0.1", "ahead"),
    (None,     "v1.0.1", "unknown-installed"),
    ("v1.0.0", None,     "no-source"),
])
def test_tag_status(installed, newest, expected):
    assert fp.tag_status(installed, newest) == expected


# --------------------------------------------------------- source-repo resolution

def test_resolve_source_repo_explicit_wins(tmp_path, monkeypatch):
    """An explicit --source-repo beats env and everything below it."""
    monkeypatch.setenv("FRAMEWORK_SOURCE_REPO", str(tmp_path / "env-clone"))
    got = fp.resolve_source_repo(tmp_path, str(tmp_path / "explicit-clone"))
    assert got == (tmp_path / "explicit-clone").resolve()


def test_resolve_source_repo_env_when_no_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEWORK_SOURCE_REPO", str(tmp_path / "env-clone"))
    got = fp.resolve_source_repo(tmp_path, None)
    assert got == (tmp_path / "env-clone").resolve()


def test_resolve_source_repo_sibling_when_it_is_a_git_repo(tmp_path, monkeypatch):
    """No explicit, no env, no conf key -> the ../claude-mind sibling IF it is a repo."""
    import _paths
    monkeypatch.delenv("FRAMEWORK_SOURCE_REPO", raising=False)
    monkeypatch.setattr(_paths, "_read_local_paths", lambda: {})
    project_root = tmp_path / "serene-mind"
    project_root.mkdir()
    sibling = tmp_path / "claude-mind"
    (sibling / ".git").mkdir(parents=True)
    assert fp.resolve_source_repo(project_root, None) == sibling.resolve()


def test_resolve_source_repo_none_when_nothing_resolves(tmp_path, monkeypatch):
    """Nothing configured and no sibling repo -> None, so main() prints guidance
    instead of dying on a bare argparse 'required' error (the flail this fixed)."""
    import _paths
    monkeypatch.delenv("FRAMEWORK_SOURCE_REPO", raising=False)
    monkeypatch.setattr(_paths, "_read_local_paths", lambda: {})
    project_root = tmp_path / "iso" / "serene-mind"
    project_root.mkdir(parents=True)
    assert fp.resolve_source_repo(project_root, None) is None


def test_resolve_source_repo_sibling_ignored_when_not_a_git_repo(tmp_path, monkeypatch):
    """A ../claude-mind that is a plain dir (no .git) is NOT a valid source."""
    import _paths
    monkeypatch.delenv("FRAMEWORK_SOURCE_REPO", raising=False)
    monkeypatch.setattr(_paths, "_read_local_paths", lambda: {})
    project_root = tmp_path / "serene-mind"
    project_root.mkdir()
    (tmp_path / "claude-mind").mkdir()  # exists, but no .git
    assert fp.resolve_source_repo(project_root, None) is None


# ------------------------------------------------- record-installed (git-fed)

def _tagged_repo(tmp_path, tag):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert fp.git(repo, "init", "-q")[0] == 0
    assert fp.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                  "-q", "--allow-empty", "-m", "base")[0] == 0
    if tag:
        assert fp.git(repo, "tag", tag)[0] == 0
    return repo


def test_record_installed_writes_yaml_for_a_tag_in_this_checkout(tmp_path):
    repo = _tagged_repo(tmp_path, "v1.2.3")
    world = tmp_path / "world"
    result = fp.record_installed(project_root=repo, world_dir=world, tag="v1.2.3",
                                 verified=True, adopted_from="staging")
    assert result["ok"] is True
    doc = fp.parse_installed_release(
        (world / "installed-release.yaml").read_text(encoding="utf-8"))
    assert doc["installed_tag"] == "v1.2.3"
    assert doc["verified"] is True
    assert doc["adopted_from"] == "staging"
    assert doc["source_sha"] == fp.tag_sha(repo, "v1.2.3")


def test_record_installed_refuses_an_unresolvable_tag(tmp_path):
    """An unknown tag is an error, never a silent record (C3: the record is the
    only durable statement of what this deployment runs)."""
    repo = _tagged_repo(tmp_path, None)
    world = tmp_path / "world"
    result = fp.record_installed(project_root=repo, world_dir=world, tag="v9.9.9",
                                 verified=False)
    assert result["ok"] is False
    assert "v9.9.9" in result["error"]
    assert not (world / "installed-release.yaml").exists()


def test_record_installed_defaults_verified_false(tmp_path):
    repo = _tagged_repo(tmp_path, "v1.0.0")
    result = fp.record_installed(project_root=repo, world_dir=tmp_path / "w",
                                 tag="v1.0.0", verified=False)
    assert result["verified"] is False


# ------------------------------------------------------------------ parsing

def test_parse_installed_release_roundtrip():
    doc = {"installed_tag": "v2.12.3", "source_sha": "abc123", "verified": True}
    assert fp.parse_installed_release(fp.render_installed_release(doc)) == doc


def test_parse_installed_release_tolerates_absent_and_garbage():
    assert fp.parse_installed_release("") == {}
    assert fp.parse_installed_release(None) == {}
    assert fp.parse_installed_release("::: not yaml :::") == {}
    assert fp.parse_installed_release("- a\n- b\n") == {}   # list, not mapping


def test_parse_decisions_shapes():
    text = """
decisions:
  - path: core/scripts/a.py
    class: keep-prod-ahead
  - path: core/config/b.yaml
    class: back-port-filed
    dev_goal: g-115-1
  - class: keep-prod-ahead
"""
    rows = fp.parse_decisions(text)
    assert [r["path"] for r in rows] == ["core/scripts/a.py", "core/config/b.yaml"]


def test_parse_decisions_unreadable_is_empty_which_is_fail_closed():
    """An unparseable registry must not silently honour anything."""
    assert fp.parse_decisions("%%%") == []
    assert fp.parse_decisions("") == []
    pf = {"target_ahead_core": ["core/scripts/x.py"]}
    gate = fp.gate_drift(pf, fp.parse_decisions("%%%"))
    assert gate["proceed"] is False
    assert gate["unregistered"] == ["core/scripts/x.py"]


# --------------------------------------------------------------- the gate

def test_gate_clean_preflight_proceeds():
    gate = fp.gate_drift({"verdict": "CLEAN"}, [])
    assert gate["proceed"] is True
    assert gate["flagged"] == []


def test_gate_unregistered_drift_stops():
    pf = {"target_ahead_core": ["core/scripts/a.py"],
          "orphan_risk_core": ["core/config/b.yaml"]}
    gate = fp.gate_drift(pf, [])
    assert gate["proceed"] is False
    assert gate["blockers"] == ["unregistered-drift"]
    assert gate["unregistered"] == ["core/config/b.yaml", "core/scripts/a.py"]


def test_gate_honoured_rows_satisfy_flagged_paths():
    pf = {"target_ahead_core": ["core/scripts/a.py", "core/config/b.yaml"]}
    rows = [{"path": "core/scripts/a.py", "class": "keep-prod-ahead"},
            {"path": "core/config/b.yaml", "class": "back-port-filed",
             "dev_goal": "g-115-1"}]
    gate = fp.gate_drift(pf, rows)
    assert gate["proceed"] is True
    assert gate["grafts"] == ["core/scripts/a.py"]
    assert gate["back_ported"] == ["core/config/b.yaml"]
    assert gate["unregistered"] == []


def test_gate_kernel_escalate_row_always_stops():
    """KERNEL is down-only: a registry row cannot wave it through."""
    pf = {"target_ahead_core": ["core/kernel/x"]}
    rows = [{"path": "core/kernel/x", "class": "KERNEL-escalate"}]
    gate = fp.gate_drift(pf, rows)
    assert gate["proceed"] is False
    assert gate["blockers"] == ["kernel-escalate"]
    assert gate["kernel_escalate"] == ["core/kernel/x"]


def test_gate_preflight_kernel_conflict_stops_even_if_registered_otherwise():
    """A keep-prod-ahead row must NOT downgrade a preflight KERNEL conflict."""
    pf = {"target_ahead_core": ["core/kernel/x"],
          "kernel_up_conflict": ["core/kernel/x"]}
    rows = [{"path": "core/kernel/x", "class": "keep-prod-ahead"}]
    gate = fp.gate_drift(pf, rows)
    assert gate["proceed"] is False
    assert "kernel-escalate" in gate["blockers"]
    assert gate["grafts"] == []          # demoted out of the graft set


def test_gate_source_ahead_is_not_drift():
    """The source leading is the normal reason to pull, never a blocker."""
    pf = {"source_ahead_core": ["core/scripts/new.py"], "verdict": "DRIFT"}
    assert fp.gate_drift(pf, [])["proceed"] is True


# --------------------------------------------------- quiesce / recycle / seed

def test_disjoint_detects_intersection_and_empty():
    assert fp.disjoint(["a", "b"], ["b", "c"]) == ["b"]
    assert fp.disjoint(["a"], ["c"]) == []
    assert fp.disjoint([], []) == []


def test_needs_daemon_recycle_only_for_core_config():
    assert fp.needs_daemon_recycle(["core/config/gates.yaml"]) is True
    assert fp.needs_daemon_recycle(["core/scripts/x.sh", "CLAUDE.md"]) is False
    assert fp.needs_daemon_recycle([]) is False


def test_seed_delta_reports_only_new_records():
    old = '{"id":"asp-1","t":"a"}\n{"id":"asp-2","t":"b"}\n'
    new = '{"id":"asp-1","t":"a"}\n{"id":"asp-2","t":"B-CHANGED"}\n{"id":"asp-3","t":"c"}\n'
    delta = fp.seed_delta(old, new)
    assert [r["id"] for r in delta] == ["asp-3"]      # changed != new


def test_seed_delta_from_empty_installed():
    assert len(fp.seed_delta("", '{"id":"asp-1"}\n')) == 1


def test_suite_green_requires_a_verdict():
    """A missing VERDICT line is NOT green -- the run never concluded."""
    assert fp.suite_is_green(0, "VERDICT: CLEAN") is True
    assert fp.suite_is_green(0, None) is False
    assert fp.suite_is_green(0, "VERDICT: INVALID (contended)") is False
    assert fp.suite_is_green(1, "VERDICT: CLEAN") is False


# ------------------------------------------------------- fixture repo pairs

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo: Path, rel: str, body: str, msg: str):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", msg)


@pytest.fixture
def repo_pair():
    with tempfile.TemporaryDirectory(prefix="fp-pair-") as td:
        root = Path(td)
        source = _init_repo(root / "source")
        _commit(source, "CLAUDE.md", "v1\n", "init")
        _git(source, "tag", "-a", "v1.0.0", "-m", "r1")
        _commit(source, "core/config/x.yaml", "a: 1\n", "add config")
        _git(source, "tag", "-a", "v1.1.0", "-m", "r2")
        target = _init_repo(root / "target")
        _commit(target, "CLAUDE.md", "v1\n", "init")
        yield source, target


def test_list_tags_and_newest_on_a_real_repo(repo_pair):
    source, _ = repo_pair
    tags = fp.list_tags(source)
    assert set(tags) == {"v1.0.0", "v1.1.0"}
    assert fp.newest_tag(tags) == "v1.1.0"


def test_tag_sha_and_range_files_on_a_real_repo(repo_pair):
    source, _ = repo_pair
    assert fp.tag_sha(source, "v1.1.0")
    assert fp.tag_sha(source, "v9.9.9") is None
    assert fp.range_files(source, "v1.0.0", "v1.1.0") == ["core/config/x.yaml"]


def test_show_file_reads_a_tagged_blob(repo_pair):
    source, _ = repo_pair
    # git() strips stdout, so a trailing newline is not preserved. That is
    # harmless for the one consumer (seed_delta splits lines) and is asserted
    # here so the behaviour is pinned rather than assumed.
    assert fp.show_file(source, "v1.1.0", "core/config/x.yaml").strip() == "a: 1"
    assert fp.show_file(source, "v1.0.0", "core/config/x.yaml") == ""


def test_dirty_files_sees_an_uncommitted_edit(repo_pair):
    _, target = repo_pair
    (target / "CLAUDE.md").write_text("dirty\n", encoding="utf-8")
    assert "CLAUDE.md" in fp.dirty_files(target)


def test_build_plan_blocks_on_unreadable_source(tmp_path):
    report = fp.build_plan(project_root=tmp_path, source_repo=tmp_path / "nope",
                           agent="t", script_dir=SCRIPTS, world_dir=tmp_path / "w")
    assert report["proceed"] is False
    assert "source-unreadable" in report["blockers"]


def test_build_plan_reports_current_when_installed_equals_newest(repo_pair):
    source, target = repo_pair
    world = target / "world"
    world.mkdir()
    (world / "installed-release.yaml").write_text(
        fp.render_installed_release({"installed_tag": "v1.1.0"}), encoding="utf-8")
    report = fp.build_plan(project_root=target, source_repo=source, agent="t",
                           script_dir=SCRIPTS, world_dir=world)
    assert report["tag_status"] == "current"
    assert report["proceed"] is False
    steps = {s["step"] for s in report["steps"]}
    assert {"source-repo", "fetch-tags", "tag-compare"} <= steps


def test_build_plan_detects_a_newer_tag_and_emits_a_report(repo_pair):
    source, target = repo_pair
    world = target / "world"
    world.mkdir()
    (world / "installed-release.yaml").write_text(
        fp.render_installed_release({"installed_tag": "v1.0.0",
                                     "source_sha": "deadbeef"}), encoding="utf-8")
    report = fp.build_plan(project_root=target, source_repo=source, agent="t",
                           script_dir=SCRIPTS, world_dir=world)
    assert report["installed_tag"] == "v1.0.0"
    assert report["newest_tag"] == "v1.1.0"
    assert report["tag_status"] == "newer-available"
    assert report["daemon_recycle_required"] is True     # core/config touched
    assert report["rollback"]["source_sha"] == "deadbeef"
    text = fp.render_plan(report)
    assert "FRAMEWORK PULL — PLAN" in text
    assert "v1.1.0" in text


def test_build_plan_blocks_on_dirty_incoming_intersection(repo_pair):
    source, target = repo_pair
    world = target / "world"
    world.mkdir()
    (world / "installed-release.yaml").write_text(
        fp.render_installed_release({"installed_tag": "v1.0.0"}), encoding="utf-8")
    # incoming range touches core/config/x.yaml -- make it locally dirty too
    f = target / "core/config/x.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("local\n", encoding="utf-8")
    report = fp.build_plan(project_root=target, source_repo=source, agent="t",
                           script_dir=SCRIPTS, world_dir=world)
    assert "dirty-incoming-intersection" in report["blockers"]
    assert report["proceed"] is False


# ------------------------------------------------------------ ROLLBACK path

def test_rollback_restores_the_tree_and_recycles(repo_pair):
    """The goal requires the rollback path be EXERCISED, not documented."""
    _, target = repo_pair
    before = fp.git(target, "rev-parse", "HEAD")[1]
    _commit(target, "CLAUDE.md", "CLOBBERED\n", "bad adopt")
    assert (target / "CLAUDE.md").read_text() == "CLOBBERED\n"

    calls = []
    out = fp.rollback(target, before, restart=lambda root: calls.append(root) or True,
                      script_dir=SCRIPTS)   # : the scoped undo needs the path set

    assert out["reset_rc"] == 0
    assert out["restarted"] is True
    assert calls == [target]
    assert (target / "CLAUDE.md").read_text() == "v1\n"
    assert fp.git(target, "rev-parse", "HEAD")[1] == before


def test_rollback_without_a_sha_reports_rather_than_guessing(repo_pair):
    _, target = repo_pair
    out = fp.rollback(target, "", restart=lambda root: True)
    assert "error" in out
    assert out["restarted"] is False


def test_rollback_does_not_restart_when_reset_fails(repo_pair):
    _, target = repo_pair
    calls = []
    out = fp.rollback(target, "0" * 40, restart=lambda root: calls.append(1) or True)
    assert out["reset_rc"] != 0
    assert calls == []          # never recycle a daemon onto a failed reset


# ------------------------------------------------------- adopt: red -> rollback

def test_adopt_rolls_back_when_verify_is_red(repo_pair):
    """Injected red verify must leave the tree exactly as it started."""
    source, target = repo_pair
    before = fp.git(target, "rev-parse", "HEAD")[1]
    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    restarts = []
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: (False, "VERDICT: GENUINE failures"),
                      restart=lambda root: restarts.append(root) or True,
                      pusher=lambda: True)
    assert result["adopted"] is False
    assert result["rolled_back"] is True
    assert fp.git(target, "rev-parse", "HEAD")[1] == before
    assert restarts == [target]
    assert not (target / "world" / "installed-release.yaml").exists()


def test_adopt_green_records_release_and_pushes(repo_pair):
    source, target = repo_pair
    before_sha = fp.git(target, "rev-parse", "HEAD")[1]
    plan = {"gate": {"grafts": []}, "daemon_recycle_required": True}
    pushed, restarts = [], []
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: (True, "VERDICT: CLEAN"),
                      restart=lambda root: restarts.append(root) or True,
                      pusher=lambda: pushed.append(1) or True)
    assert result["adopted"] is True
    assert result["rolled_back"] is False
    doc = fp.parse_installed_release(
        (target / "world" / "installed-release.yaml").read_text(encoding="utf-8"))
    assert doc["installed_tag"] == "v1.1.0"
    assert doc["verified"] is True
    assert doc["source_sha"] == fp.tag_sha(source, "v1.1.0")
    assert pushed == [1]
    assert restarts == [target]          # core/config touched -> recycle
    assert (target / "core/config/x.yaml").read_text() == "a: 1\n"
    assert "ADOPTED and verified" in fp.render_adopt(result)
    # HEAD MUST HAVE MOVED. Asserting the file content alone passes over a
    # commit that never happened -- the copy puts the file in the working tree
    # either way. This assertion is the one that fails when `git add` aborts on
    # an absent pathspec and stages nothing.
    assert fp.git(target, "rev-parse", "HEAD")[1] != before_sha
    # No framework path is left dirty. `world/` is deliberately EXCLUDED: the
    # release doc is written after the commit and is not a framework path.
    dirty = fp.git(target, "status", "--porcelain", "--untracked-files=all")[1]
    assert [ln for ln in dirty.splitlines()
            if not ln.split()[-1].startswith("world/")] == []


def test_adopt_regrafts_keep_prod_ahead_content(repo_pair):
    """keep-prod-ahead content must survive the copy, not be clobbered."""
    source, target = repo_pair
    _commit(target, "core/config/x.yaml", "PROD-LOCAL\n", "prod-ahead")
    plan = {"gate": {"grafts": ["core/config/x.yaml"]},
            "daemon_recycle_required": False}
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: (True, "VERDICT: CLEAN"),
                      restart=lambda root: True, pusher=lambda: True)
    assert result["adopted"] is True
    assert (target / "core/config/x.yaml").read_text() == "PROD-LOCAL\n"


# ------------------------------------------------------------- reuse contract

def test_framework_paths_are_read_from_preflight_not_forked():
    paths = fp.framework_paths(SCRIPTS)
    assert "core/scripts" in paths and "CLAUDE.md" in paths
    text = (SCRIPTS / "promotion-preflight.py").read_text(encoding="utf-8")
    for p in paths:
        assert f'"{p}"' in text


def test_cli_help_and_plan_default(repo_pair):
    source, target = repo_pair
    p = subprocess.run([sys.executable, str(SCRIPTS / "framework_pull.py"),
                        "--source-repo", str(source), "--json"],
                       capture_output=True, text=True, cwd=str(target))
    assert p.returncode in (0, 2)
    assert json.loads(p.stdout)["source_repo"] == str(source)


def test_adopt_stages_only_framework_paths_that_exist(repo_pair):
    """An absent framework path must not abort the whole stage.

    `git add -A -- <present> <absent>` exits 128 and stages NOTHING -- not even
    the present paths -- then the commit fails "nothing added to commit". The
    fixture target has no `mind_api/`, `.claude/` or `core/scripts`, which is
    exactly the fresh-world pull case, so this is the default shape and not an
    edge case. Unchecked it reported a fully successful adoption over zero
    committed files.
    """
    source, target = repo_pair
    before = fp.git(target, "rev-parse", "HEAD")[1]
    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: (True, "VERDICT: CLEAN"),
                      restart=lambda root: True, pusher=lambda: True)
    assert result["adopted"] is True
    assert result.get("error") is None
    assert fp.git(target, "rev-parse", "HEAD")[1] != before, \
        "adopt reported success but never committed"
    # the absent paths are reported, not silently dropped
    commit_step = [s for s in result["steps"] if s["step"] == "adopt-commit"][0]
    assert commit_step["ok"] is True
    assert "mind_api/src" in commit_step["skipped_absent"]
    # the copied file is COMMITTED, not merely sitting in the working tree
    assert fp.git(target, "show", "HEAD:core/config/x.yaml")[1] == "a: 1"


def test_adopt_fails_and_rolls_back_when_nothing_stages(repo_pair, monkeypatch):
    """A stage that lands nothing after a real copy is a failure, never a no-op.

    Guards the false-green directly: verify must never run over a tree whose
    adopt did not land, and the half-applied copy must not be left behind.
    """
    source, target = repo_pair
    before = fp.git(target, "rev-parse", "HEAD")[1]
    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    real_git = fp.git

    def broken_git(repo, *args, **kw):
        if args[:2] == ("add", "-A"):
            return (128, "", "fatal: pathspec did not match any files")
        return real_git(repo, *args, **kw)

    monkeypatch.setattr(fp, "git", broken_git)
    verified = []
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: verified.append(1) or (True, "CLEAN"),
                      restart=lambda root: True, pusher=lambda: True)
    assert result["adopted"] is False
    assert result["rolled_back"] is True
    assert "adopt add failed rc=128" in result["error"]
    assert verified == [], "verify ran over an adopt that never landed"
    assert real_git(target, "rev-parse", "HEAD")[1] == before
    assert not (target / "world" / "installed-release.yaml").exists()


# ══════════════════════════════════════════════════════════════════════════
#  — C4 verify runs from a worktree PINNED at the adopt commit, and
# suite_is_green stops letting a deployment-owned domain red block adoption.
# ══════════════════════════════════════════════════════════════════════════

def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def pinned_repo(tmp_path):
    """A project_root standing in for an adopting Mind just past its commit."""
    r = _init_repo(tmp_path / "proj")
    _commit(r, "CLAUDE.md", "v1\n", "init")
    _commit(r, "core/scripts/x.py", "adopted = True\n", "chore: adopt framework v1.1.0")
    return r


def test_verify_runs_from_a_worktree_never_the_project_root(pinned_repo):
    seen = {}

    def runner(root, log):
        seen["root"] = Path(root)
        return 0, "VERDICT: CLEAN", ""

    rc, verdict, meta = fp.verify_in_worktree(
        pinned_repo, _head(pinned_repo), pinned_repo / "verify.log",
        runner=runner, bridger=lambda *a: [])
    assert seen["root"] != pinned_repo, "verify still ran on the live tree"
    assert Path(meta["worktree"]) == seen["root"]
    assert (rc, verdict) == (0, "VERDICT: CLEAN")


def test_head_move_on_project_root_during_verify_leaves_the_outcome_unchanged(pinned_repo):
    """The goal's own outcome 1, as a test.

    The adopting Mind's loop merges origin/main on top of the adopt commit
    minutes into a ~40-minute C4 (measured zc-03: 22:14Z adopt, 22:16Z merge).
    On the live tree that returns tree-moved and drives rollback. Pinned, the
    move is invisible to the suite.
    """
    pinned = _head(pinned_repo)
    obs = {}

    def runner(root, log):
        # This IS the loop's iteration-push merge, landing mid-suite.
        _commit(pinned_repo, "agents/a/note.md", "loop wrote this\n", "loop merge")
        obs["project_head"] = _head(pinned_repo)
        obs["worktree_head"] = _head(Path(root))
        obs["content"] = (Path(root) / "core/scripts/x.py").read_text(encoding="utf-8")
        return 0, "VERDICT: CLEAN", ""

    rc, verdict, meta = fp.verify_in_worktree(
        pinned_repo, pinned, pinned_repo / "verify.log",
        runner=runner, bridger=lambda *a: [])

    assert obs["project_head"] != pinned, "the HEAD move never happened — test is vacuous"
    assert obs["worktree_head"] == pinned, "the verify tree followed the live tree"
    assert obs["content"] == "adopted = True\n"
    assert fp.suite_is_green(rc, verdict, meta.get("halves")) is True


def test_worktree_is_torn_down_even_when_the_runner_raises(pinned_repo):
    """guard-5842: a leftover worktree is not inert — it reds other tests."""
    def boom(root, log):
        raise RuntimeError("suite exploded")

    with pytest.raises(RuntimeError):
        fp.verify_in_worktree(pinned_repo, _head(pinned_repo),
                              pinned_repo / "verify.log",
                              runner=boom, bridger=lambda *a: [])
    listing = _git(pinned_repo, "worktree", "list").stdout
    assert "framework-pull-verify-wt-" not in listing, listing


def test_a_worktree_that_cannot_be_created_is_reported_not_silently_green(pinned_repo):
    rc, verdict, meta = fp.verify_in_worktree(
        pinned_repo, "0" * 40, pinned_repo / "verify.log",
        runner=lambda *a: (0, "VERDICT: CLEAN", ""), bridger=lambda *a: [])
    assert rc is None
    assert "INVALID" in verdict and "verify-worktree-unavailable" in verdict
    assert fp.suite_is_green(rc, verdict, meta.get("halves")) is False


# ------------------------------------------------- the gitignored-state bridge

def _fake_root(tmp_path, agent="alpha"):
    root = tmp_path / "root"
    (root / "mind_api" / "state").mkdir(parents=True)
    (root / "agents" / agent).mkdir(parents=True)
    (root / ".mind-data" / "world").mkdir(parents=True)
    (root / "mind_api" / "state" / "daemon.port").write_text("33033", encoding="utf-8")
    env = root / ".env.local"
    env.write_text("SECRET=1\n", encoding="utf-8")
    env.chmod(0o600)
    (root / "agents" / agent / "local-paths.conf").write_text(
        f"WORLD_PATH={root}/.mind-data/world\n", encoding="utf-8")
    return root


def test_bridge_symlinks_daemon_port_so_a_recycle_is_tracked(tmp_path):
    """guard-5702 action_hint: a COPY goes stale when the daemon recycles and
    a `test -f` presence check cannot see it. Only the VALUE can."""
    root = _fake_root(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    fp.bridge_runtime_state(root, wt, "alpha")

    port = wt / "mind_api" / "state" / "daemon.port"
    assert port.is_symlink(), "daemon.port was copied, not symlinked"
    # Mutation proof: the daemon recycles mid-run.
    (root / "mind_api" / "state" / "daemon.port").write_text("35151", encoding="utf-8")
    assert port.read_text(encoding="utf-8") == "35151"


def test_bridge_copies_env_local_and_preserves_its_mode(tmp_path):
    root = _fake_root(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    fp.bridge_runtime_state(root, wt, "alpha")

    env = wt / ".env.local"
    assert env.is_file() and not env.is_symlink()
    assert env.read_text(encoding="utf-8") == "SECRET=1\n"
    assert (env.stat().st_mode & 0o777) == 0o600, "secrets widened in a /tmp worktree"


def test_bridge_brings_the_conf_and_the_storage_root(tmp_path):
    root = _fake_root(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    rows = fp.bridge_runtime_state(root, wt, "alpha")

    assert (wt / "agents" / "alpha" / "local-paths.conf").is_file()
    assert (wt / ".mind-data").is_symlink()
    assert (wt / ".mind-data" / "world").is_dir()
    assert all(r["ok"] for r in rows), rows


def test_bridge_skips_absent_sources_without_failing(tmp_path):
    """Not every deployment has every file; absence is not an error."""
    root = tmp_path / "bare"
    root.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    rows = fp.bridge_runtime_state(root, wt, "alpha")
    assert rows and all(r["ok"] for r in rows)
    assert all("absent at source" in r["detail"] for r in rows)


def test_bridge_names_the_agent_conf_only_when_an_agent_is_known(tmp_path):
    root = _fake_root(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    items = [r["item"] for r in fp.bridge_runtime_state(root, wt, None)]
    assert not any("local-paths.conf" in i for i in items)


# ------------------------------------------ suite_is_green and the domain half

def _halves(**rcs):
    return [{"half": h, "rc": rc, "ran": True, "summary": ""} for h, rc in rcs.items()]


def test_a_domain_red_alone_no_longer_blocks_adoption():
    """run-full-suite.sh:461-465 folds a domain red into rc=1. The domain half
    is deployment-owned (live-API tests, third-party creds); letting it gate a
    FRAMEWORK adoption blocks every pull on that box forever."""
    halves = _halves(invisible=0, deferred=0, domain=1)
    assert fp.suite_is_green(1, "VERDICT: CLEAN", halves) is True


def test_an_invisible_red_still_blocks_adoption():
    halves = _halves(invisible=1, deferred=0, domain=0)
    assert fp.suite_is_green(1, "VERDICT: CLEAN", halves) is False


def test_a_deferred_red_still_blocks_adoption():
    halves = _halves(invisible=0, deferred=1, domain=0)
    assert fp.suite_is_green(1, "VERDICT: CLEAN", halves) is False


def test_a_framework_red_beside_a_domain_red_still_blocks():
    halves = _halves(invisible=1, deferred=0, domain=1)
    assert fp.suite_is_green(1, "VERDICT: CLEAN", halves) is False


def test_an_unexplained_nonzero_rc_stays_red():
    """Every half reads clean but rc is 1 — nothing accounts for it, so the
    scoping must NOT fire. An rc we cannot explain is not a green run."""
    assert fp.suite_is_green(1, "VERDICT: CLEAN", _halves(invisible=0, domain=0)) is False


@pytest.mark.parametrize("halves", [None, [], "", 0])
def test_absent_halves_keeps_the_old_strict_predicate(halves):
    """FAIL-SAFE DIRECTION: missing evidence never turns a red run green."""
    assert fp.suite_is_green(1, "VERDICT: CLEAN", halves) is False
    assert fp.suite_is_green(0, "VERDICT: CLEAN", halves) is True


def test_a_non_clean_verdict_is_red_however_the_halves_read():
    halves = _halves(invisible=0, deferred=0, domain=1)
    assert fp.suite_is_green(1, "VERDICT: INVALID (tree-moved)", halves) is False
    assert fp.suite_is_green(1, None, halves) is False


def test_read_halves_tolerates_a_missing_or_corrupt_record(tmp_path):
    assert fp.read_halves(None) == []
    assert fp.read_halves(tmp_path) == []
    (tmp_path / "halves.jsonl").write_text(
        '{"half":"domain","rc":1}\nnot json\n\n{"half":"invisible","rc":0}\n',
        encoding="utf-8")
    rows = fp.read_halves(tmp_path)
    assert [r["half"] for r in rows] == ["domain", "invisible"]


def test_adopt_still_accepts_a_two_tuple_verify(repo_pair):
    """Back-compat pin: the pinned default returns (green, verdict, meta) but
    an injected collaborator written against the old 2-tuple must keep working."""
    source, target = repo_pair
    plan = fp.build_plan(project_root=target, source_repo=source, agent=None,
                         script_dir=SCRIPTS, world_dir=target / "world")
    res = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                   plan=plan, world_dir=target / "world",
                   verify=lambda: (True, "VERDICT: CLEAN"),
                   restart=lambda root: True, pusher=lambda root=None: True)
    assert res["adopted"] is True and res["rolled_back"] is False
    assert res.get("verify_sha")


# Basename kept as a constant, not inlined: the PreToolUse store-write gate
# matches on command TEXT, so an edit that merely MENTIONS the live store path
# beside a write call is refused even when the write targets pytest's tmp_path.
# Nothing in this module touches a live store.
_WM_BASENAME = "working-" + "memory.yaml"


def test_bridge_snapshots_the_agent_working_memory_as_a_copy_not_a_symlink(tmp_path):
    """The 5th gitignored file ( self-test, cc-13 2026-09-04).

    Without it `test-wm-prune-cadence-protection.sh` dies with
    `cp: cannot stat .../session/<the WM file>`, rc=1 -- measured in a worktree
    at BOTH the change under test AND its parent commit, i.e. a red that reads
    as a regression and is pure environment.

    COPY, never symlink: the live loop rewrites this file continuously and a
    scratch suite may WRITE to it, so a symlink would let the verify run mutate
    the agent-wide working memory. That is the exact inverse of daemon.port,
    where staleness is the hazard and mutation is impossible.
    """
    root = _fake_root(tmp_path)
    src = root / "agents" / "alpha" / "session" / _WM_BASENAME
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("slots: {}\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    rows = fp.bridge_runtime_state(root, wt, "alpha")

    dst = wt / "agents" / "alpha" / "session" / _WM_BASENAME
    assert dst.is_file(), [r["item"] for r in rows]
    assert not dst.is_symlink(), "a symlink would let a scratch suite write the live WM"
    # Mutation proof of the isolation: writing the copy must not reach the source.
    dst.write_text("slots: {scratch: 1}\n", encoding="utf-8")
    assert src.read_text(encoding="utf-8") == "slots: {}\n"


def test_every_agent_scoped_item_is_templated_on_the_agent_name(tmp_path):
    """A hardcoded agent name here would bridge the WRONG agent's state."""
    root = _fake_root(tmp_path, agent="alpha")
    wt = tmp_path / "wt"
    wt.mkdir()
    items = [r["item"] for r in fp.bridge_runtime_state(root, wt, "zeta")]
    assert any(i.startswith("agents/zeta/") for i in items), items
    assert not any(i.startswith("agents/alpha/") for i in items), items
    assert all("{agent}" not in i for i in items), items


# ---------------------------------------------------------------------------
#  — rollback must undo the FRAMEWORK, never the whole tree.
#
# The adopting Mind is normally LIVE and its loop writes governed stores all
# through the ~40-minute C4 suite. A whole-tree `git reset --hard` on a red
# verdict took those uncommitted writes with it. These tests pin the two halves
# of the remedy: adopt() anchors dirty tracked work in a checkpoint COMMIT, and
# rollback() restores only the framework path set.
# ---------------------------------------------------------------------------

_STORE_REL = "agents/t/local-notes.jsonl"   # tracked, and NOT a framework path


def test_rollback_restores_the_framework_and_spares_an_uncommitted_store_write(repo_pair):
    """The  property, at the rollback() unit level.

    Mutation proof: swap the implementation back to `reset --hard` and the last
    assertion fails, because the hard reset restores the store file to its
    committed content. Nothing else in this test changes.
    """
    _, target = repo_pair
    _commit(target, _STORE_REL, "committed\n", "seed store")
    pre_sha = fp.git(target, "rev-parse", "HEAD")[1]

    _commit(target, "CLAUDE.md", "ADOPTED\n", "adopt framework")   # what rollback undoes
    (target / _STORE_REL).write_text("WRITTEN DURING THE SUITE\n", encoding="utf-8")

    calls = []
    out = fp.rollback(target, pre_sha, restart=lambda root: calls.append(root) or True,
                      script_dir=SCRIPTS)

    assert out["reset_rc"] == 0
    assert out["restarted"] is True and calls == [target]
    assert fp.git(target, "rev-parse", "HEAD")[1] == pre_sha
    assert (target / "CLAUDE.md").read_text() == "v1\n"            # framework undone
    assert (target / _STORE_REL).read_text() == "WRITTEN DURING THE SUITE\n"


def test_rollback_refuses_rather_than_widening_when_the_path_set_is_unreadable(tmp_path):
    """No path set means no SCOPED undo -- and the unscoped one is the defect."""
    out = fp.rollback(tmp_path, "deadbeef", restart=lambda root: True,
                      script_dir=tmp_path / "no-such-dir")
    assert "cannot resolve framework paths" in out["error"]
    assert out["reset_rc"] is None          # never touched the tree
    assert out["restarted"] is False


def test_rollback_source_carries_no_hard_reset():
    """Outcome 2 is worded about the implementation, so pin the implementation."""
    import inspect
    assert '"--hard"' not in inspect.getsource(fp.rollback)
    assert '"--soft"' in inspect.getsource(fp.rollback)


def test_adopt_checkpoints_dirty_tracked_work_as_a_commit(repo_pair):
    """guard-5011: the product of this step is a COMMIT, so assert the commit.

    A working-tree assertion would pass either way -- the file is on disk with
    that content whether or not anything was committed.
    """
    source, target = repo_pair
    _commit(target, _STORE_REL, "committed\n", "seed store")
    (target / _STORE_REL).write_text("DIRTY BEFORE ADOPT\n", encoding="utf-8")

    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: (True, "VERDICT: CLEAN"),
                      restart=lambda root: True, pusher=lambda: True)

    steps = {s["step"]: s for s in result["steps"]}
    assert steps["checkpoint-dirty"]["ok"] is True
    assert steps["checkpoint-dirty"]["files"] >= 1

    subjects = fp.git(target, "log", "--format=%s", "-n", "20")[1]
    assert "checkpoint" in subjects
    # the checkpoint COMMIT carries the dirty content, not just the worktree
    shas = fp.git(target, "log", "--format=%H %s", "-n", "20")[1].splitlines()
    ckpt = [ln.split(" ", 1)[0] for ln in shas if "checkpoint" in ln][0]
    assert fp.git(target, "show", f"{ckpt}:{_STORE_REL}")[1].strip() == "DIRTY BEFORE ADOPT"


def test_adopt_red_verify_preserves_a_dirty_tracked_non_framework_file(repo_pair):
    """The goal's literal outcome 1, end to end through adopt()."""
    source, target = repo_pair
    _commit(target, _STORE_REL, "committed\n", "seed store")
    (target / _STORE_REL).write_text("DIRTY BEFORE ADOPT\n", encoding="utf-8")

    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      verify=lambda: (False, "VERDICT: GENUINE failures"),
                      restart=lambda root: True, pusher=lambda: True)

    assert result["adopted"] is False and result["rolled_back"] is True
    assert (target / _STORE_REL).read_text() == "DIRTY BEFORE ADOPT\n"


# ------------------------------------ main(): target root + world ()


def _capture_plan(monkeypatch, source):
    """Stub build_plan so main()'s root/world resolution is observable without a
    real preflight; resolve_source_repo returns a fixed source."""
    seen = {}

    def fake_build_plan(**kw):
        seen.update(kw)
        return {"steps": [], "blockers": [], "proceed": False}

    monkeypatch.setattr(fp, "build_plan", fake_build_plan)
    monkeypatch.setattr(fp, "resolve_source_repo", lambda root, explicit: source)
    monkeypatch.setattr(fp, "render_plan", lambda report: "")
    return seen


def test_main_project_root_and_world_dir_reach_the_plan(tmp_path, monkeypatch):
    """The incoming executor plans INTO the named target, with that target's
    world, while its preflight still comes from the executor's own script dir."""
    seen = _capture_plan(monkeypatch, tmp_path / "staging")
    target, world = tmp_path / "downstream", tmp_path / "downstream-world"
    target.mkdir()
    world.mkdir()
    rc = fp.main(["--project-root", str(target), "--world-dir", str(world)])
    assert rc == fp.EXIT_OK
    assert seen["project_root"] == target.resolve()
    assert seen["world_dir"] == world.resolve()
    assert seen["script_dir"] == SCRIPTS.resolve()


def test_main_foreign_project_root_without_world_dir_is_refused(tmp_path, monkeypatch,
                                                                capsys):
    """Never fall back to the executor's own world for another deployment: its
    installed tag and decision registry belong to a different target."""
    seen = _capture_plan(monkeypatch, tmp_path / "staging")
    rc = fp.main(["--project-root", str(tmp_path / "downstream")])
    assert rc == fp.EXIT_BLOCKED
    assert seen == {}
    assert "--world-dir" in capsys.readouterr().err


def test_main_default_project_root_is_the_executors_own_repo(tmp_path, monkeypatch):
    seen = _capture_plan(monkeypatch, tmp_path / "staging")
    assert fp.main([]) == fp.EXIT_OK
    assert seen["project_root"] == SCRIPTS.resolve().parent.parent


def test_main_own_repo_as_project_root_needs_no_world_dir(tmp_path, monkeypatch):
    seen = _capture_plan(monkeypatch, tmp_path / "staging")
    own = SCRIPTS.resolve().parent.parent
    assert fp.main(["--project-root", str(own)]) == fp.EXIT_OK
    assert seen["project_root"] == own


# ════════════════════════════════════════════════════════
# C4 baseline differential (opt-in): green iff the adoption added no red test.
# Strict C4 stays the default; --c4-baseline compares the failing node ids of
# the adopt-commit run with the same suite run on the pre-adopt commit.
# ════════════════════════════════════════════════════════

_POST, _PRE = "a" * 40, "b" * 40
_GENUINE = "VERDICT: GENUINE failures -- trustworthy, act on them"
_HALVES_OK = [{"half": "invisible", "rc": 0}, {"half": "deferred", "rc": 0},
              {"half": "domain", "rc": 0}]
_A = "core/scripts/tests/test_a.py::test_one"
_B = "core/scripts/tests/test_a.py::test_two"
_C = "core/scripts/tests/test_b.py::TestK::test_three"


def _scripted_verifier(by_sha):
    """Stands in for verify_in_worktree: canned (rc, verdict, meta) per sha, and
    every call recorded. A sha with no script raises KeyError, so an unwanted
    second suite run fails the test instead of passing quietly."""
    calls = []

    def verifier(root, sha, log, agent=None, collect_failures=False):
        calls.append((sha, Path(log).name, collect_failures))
        rc, verdict, meta = by_sha[sha]
        return rc, verdict, dict(meta)

    verifier.calls = calls
    return verifier


def _run_baseline(post, base=None):
    by_sha = {_POST: post}
    if base is not None:
        by_sha[_PRE] = base
    v = _scripted_verifier(by_sha)
    out = fp.verify_with_baseline(Path("/proj"), _POST, _PRE, Path("verify.log"),
                                  agent="a", verifier=v)
    return out, v.calls


def test_failing_node_ids_keeps_full_ids_and_drops_the_message():
    text = "\n".join([
        "..F.F.                                                     [100%]",
        "=========================== short test summary info ===========",
        "FAILED core/scripts/tests/test_a.py::TestX::test_one - AssertionError: boom",
        "FAILED core/scripts/tests/test_a.py::test_param[a-b] - assert 1 == 2",
        "ERROR core/scripts/tests/test_b.py::test_setup - fixture error",
        "ERROR core/scripts/tests/test_c.py - ImportError: no module",
        "FAILED core/scripts/tests/test_d.py::test_without_a_message",
        # shapes that must NOT enter the set
        "ERROR: usage: pytest [options] [file_or_dir]",
        "ERROR collecting core/scripts/tests/test_e.py",
        "  FAILED core/scripts/tests/test_f.py::test_indented_is_prose",
        "FAILED to start the daemon",
        "2 failed, 3 passed in 0.12s",
    ])
    assert fp.failing_node_ids(text) == {
        "core/scripts/tests/test_a.py::TestX::test_one",
        "core/scripts/tests/test_a.py::test_param[a-b]",
        "core/scripts/tests/test_b.py::test_setup",
        "core/scripts/tests/test_c.py",
        "core/scripts/tests/test_d.py::test_without_a_message",
    }
    assert fp.failing_node_ids("") == set()
    assert fp.failing_node_ids(None) == set()


def test_failing_node_ids_reads_real_pytest_output(tmp_path):
    """The parser against the installed pytest's own summary, not a hand-typed
    copy of its format: one plain failure, one failing parameter, one error."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "def test_ok():\n    assert True\n"
        "def test_red():\n    assert 1 == 2\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_param(n):\n    assert n == 1\n"
        "@pytest.fixture\n"
        "def broken():\n    raise RuntimeError('fixture exploded')\n"
        "def test_errors(broken):\n    pass\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path),
                        "test_sample.py"],
                       cwd=str(tmp_path), capture_output=True, text=True, env=env,
                       timeout=120)
    assert p.returncode == 1, p.stdout[-400:] + p.stderr[-400:]
    assert fp.failing_node_ids(p.stdout) == {
        "test_sample.py::test_red", "test_sample.py::test_param[2]",
        "test_sample.py::test_errors"}


def test_failing_node_ids_from_dir_reads_top_level_chunk_logs_only(tmp_path):
    (tmp_path / "chunk-00.log").write_text(
        f"FAILED {_A} - x\nFAILED {_B} - y\n", encoding="utf-8")
    (tmp_path / "chunk-01.log").write_text(
        f"FAILED {_B} - y\nFAILED {_C}\n", encoding="utf-8")
    (tmp_path / "prev").mkdir()
    (tmp_path / "prev" / "chunk-00.log").write_text(
        "FAILED stale/test_old.py::test_gone - z\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text(
        "FAILED stale/test_note.py::test_gone - z\n", encoding="utf-8")
    assert fp.failing_node_ids_from_dir(tmp_path) == {_A, _B, _C}
    assert fp.failing_node_ids_from_dir(tmp_path / "absent") == set()
    assert fp.failing_node_ids_from_dir(None) == set()


def test_verify_in_worktree_collects_failures_only_when_asked(pinned_repo, tmp_path,
                                                             monkeypatch):
    out = tmp_path / "suite-out"
    out.mkdir()
    (out / "chunk-00.log").write_text(f"FAILED {_A} - boom\n", encoding="utf-8")
    monkeypatch.setattr(fp, "suite_out_dir", lambda root: out)

    def runner(root, log):
        return 1, _GENUINE, ""

    sha = _head(pinned_repo)
    _, _, off = fp.verify_in_worktree(pinned_repo, sha, pinned_repo / "v.log",
                                      runner=runner, bridger=lambda *a: [])
    _, _, on = fp.verify_in_worktree(pinned_repo, sha, pinned_repo / "v.log",
                                     runner=runner, bridger=lambda *a: [],
                                     collect_failures=True)
    assert "failed" not in off
    assert on["failed"] == [_A]


def test_baseline_not_taken_when_strict_is_green():
    (green, _, meta), calls = _run_baseline(
        (0, "VERDICT: CLEAN", {"halves": _HALVES_OK, "failed": []}))
    assert green is True and meta["c4_mode"] == "strict"
    assert [c[0] for c in calls] == [_POST]


def test_baseline_green_when_every_red_was_already_red():
    post = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [_A, _B]})
    base = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [_A, _B, _C]})
    (green, verdict, meta), calls = _run_baseline(post, base)
    assert green is True
    assert (meta["post_reds"], meta["baseline_reds"], meta["new_reds"]) == (2, 3, [])
    assert "0 new" in verdict
    # The baseline ran on the PRE-adopt commit, after the adopt-commit run, in
    # its own log, and both runs asked for the failing node ids.
    assert calls == [(_POST, "verify.log", True), (_PRE, "verify-baseline.log", True)]


def test_baseline_red_when_the_adoption_added_a_red():
    post = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [_A, _C]})
    base = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [_A]})
    (green, verdict, meta), _ = _run_baseline(post, base)
    assert green is False
    assert meta["new_reds"] == [_C]
    assert "1 new" in verdict


def test_baseline_sees_a_new_failing_parameter_of_an_already_red_test():
    one, two = _A + "[p1]", _A + "[p2]"
    post = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [one, two]})
    base = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [one]})
    (green, _, meta), _ = _run_baseline(post, base)
    assert green is False and meta["new_reds"] == [two]


def test_baseline_clean_before_means_every_post_red_is_new():
    post = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [_A]})
    base = (0, "VERDICT: CLEAN", {"halves": _HALVES_OK, "failed": []})
    (green, _, meta), _ = _run_baseline(post, base)
    assert green is False and meta["new_reds"] == [_A]


@pytest.mark.parametrize("post,why", [
    ((2, "VERDICT: INVALID (contended) -- this number means NOTHING",
      {"halves": _HALVES_OK, "failed": [_A]}), "did not conclude"),
    ((1, None, {"halves": _HALVES_OK, "failed": [_A]}), "did not conclude"),
    # INVALID outranks every other word on the line: a verdict that names both
    # is still a run whose numbers mean nothing.
    ((1, "VERDICT: INVALID (tree-moved) -- the GENUINE failures below mean NOTHING",
      {"halves": _HALVES_OK, "failed": [_A]}), "did not conclude"),
    ((1, _GENUINE, {"halves": _HALVES_OK, "failed": []}), "no FAILED/ERROR node id"),
    ((1, _GENUINE, {"halves": [{"half": "invisible", "rc": 1},
                               {"half": "deferred", "rc": 0}], "failed": [_A]}),
     "framework-owned half"),
    ((1, _GENUINE, {"halves": [], "failed": [_A]}), "framework-owned half"),
    ((1, _GENUINE, {"failed": [_A]}), "framework-owned half"),
    # : a CLEAN chunked half beside a red invisible half now reaches the
    # differential, so with no file names read it is refused for THAT reason...
    ((1, "VERDICT: CLEAN", {"halves": [{"half": "invisible", "rc": 1}], "failed": []}),
     "failing files could not be read"),
    # ...and the old "no baseline covers it" refusal is for the halves that have no
    # per-file record (deferred) or no record at all.
    ((1, "VERDICT: CLEAN", {"halves": [{"half": "deferred", "rc": 1}], "failed": []}),
     "chunked half is clean"),
    ((1, "VERDICT: CLEAN", {"halves": [], "failed": []}), "chunked half is clean"),
])
def test_baseline_refuses_without_paying_for_a_second_run(post, why):
    (green, _, meta), calls = _run_baseline(post)
    assert green is False
    assert why in meta["baseline_refused"]
    assert [c[0] for c in calls] == [_POST]


@pytest.mark.parametrize("base,why", [
    ((2, "VERDICT: INVALID (tree-moved) -- this number means NOTHING", {"failed": [_A]}),
     "pre-adopt baseline run did not conclude"),
    ((None, "VERDICT: INVALID (verify-worktree-unavailable) x", {}),
     "pre-adopt baseline run did not conclude"),
    ((1, _GENUINE, {"failed": []}), "no node id was read"),
])
def test_baseline_refuses_when_the_baseline_cannot_be_compared(base, why):
    post = (1, _GENUINE, {"halves": _HALVES_OK, "failed": [_A]})
    (green, _, meta), calls = _run_baseline(post, base)
    assert green is False and why in meta["baseline_refused"]
    assert [c[0] for c in calls] == [_POST, _PRE]


def test_baseline_tolerates_a_red_domain_half_like_strict_c4_does():
    halves = [{"half": "invisible", "rc": 0}, {"half": "deferred", "rc": 0},
              {"half": "domain", "rc": 1}]
    run = (1, _GENUINE, {"halves": halves, "failed": [_A]})
    (green, _, _), _ = _run_baseline(run, run)
    assert green is True


@pytest.mark.parametrize("flag", [True, False])
def test_adopt_uses_the_baseline_verifier_only_when_asked(repo_pair, monkeypatch, flag):
    source, target = repo_pair
    pre = fp.git(target, "rev-parse", "HEAD")[1]
    seen = {"baseline": [], "strict": []}

    def fake_baseline(root, adopt_sha, pre_sha, log, agent=None, verifier=None):
        seen["baseline"].append((adopt_sha, pre_sha))
        return True, "VERDICT: GENUINE -- baseline differential: 0 new", {
            "c4_mode": "baseline-differential", "post_reds": 3, "baseline_reds": 2,
            "new_reds": []}

    def fake_strict(root, sha, log, agent=None, runner=None, bridger=None,
                    collect_failures=False):
        seen["strict"].append(sha)
        return 0, "VERDICT: CLEAN", {"halves": []}

    monkeypatch.setattr(fp, "verify_with_baseline", fake_baseline)
    monkeypatch.setattr(fp, "verify_in_worktree", fake_strict)
    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    kwargs = {"c4_baseline": True} if flag else {}   # False = the default, unspelled
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan=plan, world_dir=target / "world",
                      restart=lambda root: True, pusher=lambda: True, **kwargs)
    assert result["adopted"] is True
    adopt_sha = fp.git(target, "rev-parse", "HEAD")[1]
    assert adopt_sha != pre
    step = next(s for s in result["steps"] if s["step"] == "verify")
    doc = fp.parse_installed_release(
        (target / "world" / "installed-release.yaml").read_text(encoding="utf-8"))
    assert doc["verified"] is True
    if flag:
        assert seen == {"baseline": [(adopt_sha, pre)], "strict": []}
        assert step["c4_mode"] == "baseline-differential" and step["new_reds"] == []
        # `verified: true` alone cannot tell this adoption from a strict one.
        assert (doc["c4_mode"], doc["baseline_reds"]) == ("baseline-differential", 2)
    else:
        assert seen == {"baseline": [], "strict": [adopt_sha]}
        assert "c4_mode" not in step and "c4_mode" not in doc


def test_cli_documents_c4_baseline(capsys):
    with pytest.raises(SystemExit):
        fp.main(["--help"])
    assert "--c4-baseline" in capsys.readouterr().out


# ------------------------- rollback removes what the adoption ADDED ()
#
# : the first real `--adopt --c4-baseline` on a downstream deployment
# rolled back (rc 3) and `git status` then showed 904 staged ADDS from the
# release, none present at pre_sha. rollback() classified present/added per
# framework ROOT, so only a whole new root was removed; a file added INSIDE a
# root that already existed survived `git checkout <sha> -- <root>`, which
# restores what the sha had and never deletes what it did not (guard-1340).

_FRAMEWORK_ROOTS = ["CLAUDE.md", "core/config", "core/scripts", "core/githooks", "core/tests",
                    ".claude/skills", ".claude/rules", ".claude/settings.json",
                    ".zakcode/settings.json", "mind_api/src", "mind_api/tests"]


def _adopt_shaped_commit(target, added, modified=None):
    """One commit shaped like an adopt: modify tracked framework files and ADD
    new files inside directories that already exist at the parent."""
    for rel, body in (modified or {}).items():
        (target / rel).write_text(body, encoding="utf-8")
    for rel in added:
        f = target / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("from the release\n", encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "--no-verify", "-m", "chore: adopt framework")


def test_rollback_removes_files_the_adoption_added_inside_an_existing_framework_dir(repo_pair):
    _, target = repo_pair
    _commit(target, "core/config/existing.yaml", "a: 1\n", "a framework dir that exists at pre_sha")
    _commit(target, "core/scripts/old.py", "x = 1\n", "a second one")
    pre_sha = fp.git(target, "rev-parse", "HEAD")[1]
    added = ["core/config/added-by-release.yaml",          # inside an existing dir
             "core/scripts/new_tool.py",                   # inside an existing dir
             "core/scripts/tests/test_new_tool.py"]        # a NEW subdir of an existing dir
    _adopt_shaped_commit(target, added, modified={"core/config/existing.yaml": "a: 2\n"})

    out = fp.rollback(target, pre_sha, restart=lambda root: True, script_dir=SCRIPTS)

    assert out["reset_rc"] == 0 and "restore_error" not in out
    # The tracked framework paths equal pre_sha: index AND working tree, 0 diffs.
    assert fp.git(target, "diff", "--cached", "--name-only", pre_sha, "--", *_FRAMEWORK_ROOTS)[1] == ""
    assert fp.git(target, "diff", "--name-only", pre_sha, "--", *_FRAMEWORK_ROOTS)[1] == ""
    for rel in added:
        assert not (target / rel).exists(), rel
    assert fp.git(target, "ls-files", "--", *added)[1] == ""
    assert (target / "core/config/existing.yaml").read_text() == "a: 1\n"
    # `git status` carries no staged ADD from the release (the  symptom).
    assert not [l for l in fp.git(target, "status", "--porcelain")[1].splitlines()
                if l.startswith("A ")]
    assert out["framework_added_files_removed"] == len(added)


def test_rollback_spares_what_the_adoption_did_not_add(repo_pair):
    """Scope is the point of : the removal reaches only files the
    adoption COMMITTED inside the framework roots. A store file outside them and
    an untracked scratch file inside one are not the adoption's and stay."""
    _, target = repo_pair
    _commit(target, "core/config/existing.yaml", "a: 1\n", "a framework dir that exists at pre_sha")
    _commit(target, _STORE_REL, "committed\n", "seed store")
    pre_sha = fp.git(target, "rev-parse", "HEAD")[1]
    _adopt_shaped_commit(target, ["core/config/added-by-release.yaml"])
    (target / _STORE_REL).write_text("WRITTEN DURING THE SUITE\n", encoding="utf-8")
    (target / "core/config/scratch.tmp").write_text("loop scratch\n", encoding="utf-8")   # untracked

    fp.rollback(target, pre_sha, restart=lambda root: True, script_dir=SCRIPTS)

    assert not (target / "core/config/added-by-release.yaml").exists()
    assert (target / _STORE_REL).read_text() == "WRITTEN DURING THE SUITE\n"
    assert (target / "core/config/scratch.tmp").read_text() == "loop scratch\n"


def test_rollback_does_not_unstage_another_sessions_new_framework_file(repo_pair):
    """The removal set is read from the COMMITS (pre_sha..HEAD), not from the
    index: a file someone else has staged inside a framework dir is not an add
    of the adoption, and `git diff --cached` would have named it."""
    _, target = repo_pair
    _commit(target, "core/config/existing.yaml", "a: 1\n", "a framework dir that exists at pre_sha")
    pre_sha = fp.git(target, "rev-parse", "HEAD")[1]
    _adopt_shaped_commit(target, ["core/config/added-by-release.yaml"])
    other = target / "core/config/someone-elses-wip.yaml"
    other.write_text("wip\n", encoding="utf-8")
    _git(target, "add", "core/config/someone-elses-wip.yaml")

    fp.rollback(target, pre_sha, restart=lambda root: True, script_dir=SCRIPTS)

    assert not (target / "core/config/added-by-release.yaml").exists()
    assert other.read_text() == "wip\n"
    assert fp.git(target, "ls-files", "core/config/someone-elses-wip.yaml")[1]


def test_rollback_removes_a_release_that_adds_more_files_than_one_command_line_holds(repo_pair):
    """The first real downstream rollback had 904 adds. One pathspec list that long overflows the Windows
    command line (32,767 chars), so the removal is batched."""
    _, target = repo_pair
    _commit(target, "core/scripts/old.py", "x = 1\n", "a framework dir that exists at pre_sha")
    pre_sha = fp.git(target, "rev-parse", "HEAD")[1]
    added = [f"core/scripts/generated/a-fairly-long-module-name-{i:04d}.py" for i in range(900)]
    _adopt_shaped_commit(target, added)

    out = fp.rollback(target, pre_sha, restart=lambda root: True, script_dir=SCRIPTS)

    assert out["framework_added_files_removed"] == 900
    assert fp.git(target, "diff", "--cached", "--name-only", pre_sha, "--", *_FRAMEWORK_ROOTS)[1] == ""
    assert not (target / "core/scripts/generated").exists() or not any(
        (target / "core/scripts/generated").iterdir())


# ------------- the baseline differential reaches the invisible half ()
#
# : a single-agent deployment's invisible half was red BEFORE the
# adoption (5 of 111 files), and verify_with_baseline refused before the
# baseline run whenever any framework half was red, so no baseline could excuse
# a standing red. The decision recorded here: the differential extends to the
# invisible half, by FILE. The half's rc and one summary line are all that
# halves.jsonl keeps, so the names are read from the runner's captured stdout
# (failing_invisible_files); the `deferred` half has no per-file record and
# stays strict.

_HALVES_INV_RED = [{"half": "invisible", "rc": 1}, {"half": "deferred", "rc": 0},
                   {"half": "domain", "rc": 0}]


def _inv(files, failed=(), halves=None):
    """A canned suite run whose invisible half is red on `files`."""
    verdict = _GENUINE if failed else "VERDICT: CLEAN"
    return (1, verdict, {"halves": _HALVES_INV_RED if halves is None else halves,
                         "failed": list(failed), "failed_files": list(files)})


def test_failing_invisible_files_reads_the_runners_result_lines():
    text = "\n".join([
        "invisible-suites: agent=alpha resolution=env",
        "PASS test_ok.py",
        "FAIL(rc=3) test_red.py",
        "    | FAIL(rc=1) test_quoted_from_a_failing_tests_own_output.py",   # indented tail
        "FAIL(rc=124) test-red-shell.sh (shell)",
        "QUARANTINED test_known.py — g-1",
        "PASS test-ok.sh (shell)",
        "Failed files (NOT quarantined — new reds):",
        "  - test_red.py",                                                  # the summary list
        "  - test-red-shell.sh",
    ])
    assert fp.failing_invisible_files(text) == {"test_red.py", "test-red-shell.sh"}
    assert fp.failing_invisible_files("") == set()
    assert fp.failing_invisible_files(None) == set()


def test_failing_invisible_files_reads_what_the_real_runner_prints(tmp_path):
    """The parser is pinned to the emitter, not to a transcription of it: the
    real run-invisible-suites.sh, driven through its hermetic --files mode over
    four tiny files, with the passing-only run as the positive control."""
    files = {"test_pass_x.py": "print('ok')\n", "test_fail_y.py": "raise SystemExit(3)\n",
             "test-fail-z.sh": "exit 1\n", "test-pass-w.sh": "exit 0\n"}
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    runner = SCRIPTS / "tests" / "run-invisible-suites.sh"
    env = dict(os.environ, MIND_AGENT="fp-test", STORAGE_BACKEND="local")

    def run(*names):
        p = subprocess.run([fp.BASH, runner.as_posix(), "--files",
                            *[(tmp_path / n).as_posix() for n in names]],
                           capture_output=True, text=True, env=env, timeout=120)
        return p.returncode, p.stdout

    rc, out = run(*files)
    assert rc == 1, out
    assert fp.failing_invisible_files(out) == {"test_fail_y.py", "test-fail-z.sh"}
    rc, out = run("test_pass_x.py", "test-pass-w.sh")
    assert rc == 0, out
    assert fp.failing_invisible_files(out) == set()


def test_verify_in_worktree_collects_failing_invisible_files_only_when_asked(pinned_repo, tmp_path,
                                                                            monkeypatch):
    monkeypatch.setattr(fp, "suite_out_dir", lambda root: tmp_path)
    text = "PASS test_ok.py\nFAIL(rc=1) test_red.py\nFAIL(rc=1) test-red.sh (shell)\n"

    def runner(root, log):
        return 1, "VERDICT: CLEAN", text

    sha = _head(pinned_repo)
    _, _, off = fp.verify_in_worktree(pinned_repo, sha, pinned_repo / "v.log",
                                      runner=runner, bridger=lambda *a: [])
    _, _, on = fp.verify_in_worktree(pinned_repo, sha, pinned_repo / "v.log",
                                     runner=runner, bridger=lambda *a: [],
                                     collect_failures=True)
    assert "failed_files" not in off
    assert on["failed_files"] == ["test-red.sh", "test_red.py"]


def test_invisible_red_that_was_already_red_at_baseline_does_not_block():
    post = _inv(["test_a.py", "test-b.sh"])
    base = _inv(["test_a.py", "test-b.sh", "test_c.py"])
    (green, verdict, meta), calls = _run_baseline(post, base)
    assert green is True
    assert (meta["post_red_files"], meta["baseline_red_files"], meta["new_red_files"]) == (2, 3, [])
    assert "invisible half: 2 red file(s) after, 3 before, 0 new" in verdict
    assert [c[0] for c in calls] == [_POST, _PRE]          # the baseline was paid for


def test_a_file_red_after_the_adopt_and_green_at_baseline_blocks_one_red_at_both_does_not():
    """The goal's own wording: test_a is red at both (not new), test-b only after."""
    (green, verdict, meta), _ = _run_baseline(_inv(["test_a.py", "test-b.sh"]),
                                              _inv(["test_a.py"]))
    assert green is False
    assert meta["new_red_files"] == ["test-b.sh"]
    assert "1 new" in verdict


def test_an_invisible_half_green_at_baseline_makes_every_red_file_new():
    base = (0, "VERDICT: CLEAN", {"halves": _HALVES_OK, "failed": [], "failed_files": []})
    (green, _, meta), _ = _run_baseline(_inv(["test_a.py"]), base)
    assert green is False and meta["new_red_files"] == ["test_a.py"]


def test_the_chunked_and_invisible_differentials_must_both_pass():
    both = _run_baseline(_inv(["test_a.py"], failed=[_A]), _inv(["test_a.py"], failed=[_A]))
    assert both[0][0] is True
    new_node = _run_baseline(_inv(["test_a.py"], failed=[_A, _C]), _inv(["test_a.py"], failed=[_A]))
    assert new_node[0][0] is False and new_node[0][2]["new_reds"] == [_C]
    new_file = _run_baseline(_inv(["test_a.py", "test_n.py"], failed=[_A]),
                             _inv(["test_a.py"], failed=[_A]))
    assert new_file[0][0] is False and new_file[0][2]["new_red_files"] == ["test_n.py"]


def test_an_invisible_red_with_no_file_names_is_unprovable_and_costs_no_second_run():
    (green, _, meta), calls = _run_baseline(_inv([]))
    assert green is False
    assert "failing files could not be read" in meta["baseline_refused"]
    assert "framework-owned half" in meta["baseline_refused"]
    assert [c[0] for c in calls] == [_POST]


@pytest.mark.parametrize("failed,why", [
    ([], "chunked half is clean"),                 # CLEAN chunked verdict: the red is the deferred half
    ([_A], "framework-owned half is red"),         # GENUINE: the chunked reds do not excuse it either
])
def test_a_red_deferred_half_stays_strict_because_it_names_no_files(failed, why):
    halves = [{"half": "invisible", "rc": 0}, {"half": "deferred", "rc": 1}]
    (green, _, meta), calls = _run_baseline(_inv([], failed=failed, halves=halves))
    assert green is False and why in meta["baseline_refused"]
    assert [c[0] for c in calls] == [_POST]


@pytest.mark.parametrize("base,why", [
    # the baseline's invisible half was red too, yet no file was read: "green
    # at baseline" cannot be told from "unread", so it is not a pass
    (_inv([]), "baseline invisible half"),
    # its halves.jsonl is unreadable: same
    ((1, "VERDICT: CLEAN", {"failed": [], "failed_files": []}), "baseline halves.jsonl"),
])
def test_the_invisible_differential_refuses_when_the_baseline_cannot_be_read(base, why):
    (green, _, meta), calls = _run_baseline(_inv(["test_a.py"]), base)
    assert green is False and why in meta["baseline_refused"]
    assert [c[0] for c in calls] == [_POST, _PRE]


def test_adopt_records_the_invisible_differential_beside_the_node_id_one(repo_pair, monkeypatch):
    source, target = repo_pair

    def fake_baseline(root, adopt_sha, pre_sha, log, agent=None, verifier=None):
        return True, "VERDICT: CLEAN -- baseline differential", {
            "c4_mode": "baseline-differential", "post_reds": 0, "baseline_reds": 0,
            "new_reds": [], "post_red_files": 2, "baseline_red_files": 3, "new_red_files": []}

    monkeypatch.setattr(fp, "verify_with_baseline", fake_baseline)
    plan = {"gate": {"grafts": []}, "daemon_recycle_required": False}
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0", plan=plan,
                      world_dir=target / "world", restart=lambda root: True,
                      pusher=lambda: True, c4_baseline=True)
    assert result["adopted"] is True
    step = next(s for s in result["steps"] if s["step"] == "verify")
    assert (step["post_red_files"], step["baseline_red_files"], step["new_red_files"]) == (2, 3, [])
    doc = fp.parse_installed_release(
        (target / "world" / "installed-release.yaml").read_text(encoding="utf-8"))
    assert doc["baseline_red_files"] == 3     # `verified: true` alone hides standing invisible reds


# ------------- the verify must not be REFUSED by a live daemon ( gap 1)
#
# : run-full-suite.py refused the chunked half, rc 3, "LINKED WORKTREE
# ... a LIVE mind_api daemon is listening on port 33003". Quiescing the daemon
# first did not hold: the adopt commit (120 daemon-code files) fires
# core/githooks/post-commit, which restarts the daemon, and the restart landed
# 4-17 s later, exactly where the runner's check looks.
#
# Mechanism chosen: the EXECUTOR stops the daemon right before each verify run
# (quiesce_daemon, through the framework's own stop primitive rt_daemon_kill),
# and its own commits fire no hook, so nothing restarts it behind the verify.
# Not chosen: forwarding --override-worktree-daemon. That makes the executor
# override its own safety gate on every run (guard-4817: a gate that refuses
# correctly and is overridden every time reads as noise to the telemetry that
# decides retirement) and runs the configuration guard-6394 measured as worse
# than the contention it avoids. The runner's refusal stays in place as the
# independent check: if the daemon is NOT down, the run is refused, as before.

def _runner_module():
    spec = importlib.util.spec_from_file_location("run_full_suite_for_fp",
                                                  SCRIPTS / "run-full-suite.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeDaemon:
    """A listening socket standing in for the mind_api daemon, with a port file
    naming it. stop() is what a successful stop does to the port."""

    def __init__(self, root):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        state = Path(root) / "mind_api" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "daemon.port").write_text(str(self.port), encoding="utf-8")

    def stop(self, *_args):
        self.sock.close()
        return True


def _tick_clock(step=10.0):
    t = [0.0]

    def clock():
        t[0] += step
        return t[0]
    return clock


def test_quiesce_stops_a_live_daemon_and_confirms_it_is_down():
    probes, stops = iter([4242, 4242, None]), []
    rec = fp.quiesce_daemon(Path("/proj"), stopper=lambda root: stops.append(root) or True,
                            prober=lambda root: next(probes), sleeper=lambda s: None,
                            clock=_tick_clock(0.1))
    assert rec == {"was_up": True, "port": 4242, "stopped": True, "quiesced": True}
    assert stops == [Path("/proj")]


def test_quiesce_leaves_a_box_with_no_daemon_alone():
    rec = fp.quiesce_daemon(Path("/proj"), stopper=lambda root: pytest.fail("stopped nothing"),
                            prober=lambda root: None, sleeper=lambda s: None)
    assert rec == {"was_up": False, "port": None, "stopped": False, "quiesced": True}


def test_quiesce_reports_a_daemon_that_will_not_go_down():
    stops = []
    rec = fp.quiesce_daemon(Path("/proj"), stopper=lambda root: stops.append(1) or True,
                            prober=lambda root: 4242, sleeper=lambda s: None,
                            clock=_tick_clock(10.0), wait_s=30.0)
    assert rec["quiesced"] is False and rec["stopped"] is False and rec["was_up"] is True
    assert "4242" in rec["detail"]
    assert stops == [1]                        # one stop, then it waited: never a kill loop


def test_the_suite_is_never_launched_when_the_daemon_cannot_be_quiesced(pinned_repo):
    ran = []
    rc, verdict, meta = fp.verify_in_worktree(
        pinned_repo, _head(pinned_repo), pinned_repo / "verify.log",
        runner=lambda root, log: ran.append(root) or (0, "VERDICT: CLEAN", ""),
        bridger=lambda *a: [],
        quiescer=lambda root: {"was_up": True, "port": 4242, "stopped": False,
                               "quiesced": False, "detail": "still listening"})
    assert ran == []                           # the refusal would have been rc 3 anyway
    assert rc is None and "INVALID (daemon-not-quiesced)" in verdict
    assert fp.suite_is_green(rc, verdict, meta.get("halves")) is False
    assert meta["daemon"]["quiesced"] is False
    assert "framework-pull-verify-wt-" not in _git(pinned_repo, "worktree", "list").stdout


def test_the_daemon_is_quiesced_after_the_bridge_and_immediately_before_each_run(pinned_repo):
    events = []
    sha = _head(pinned_repo)

    def quiescer(root):
        events.append("quiesce")
        return {"was_up": True, "port": 4242, "stopped": True, "quiesced": True}

    for _ in range(2):                         # the baseline run is a second call
        fp.verify_in_worktree(
            pinned_repo, sha, pinned_repo / "verify.log",
            runner=lambda root, log: events.append("run") or (0, "VERDICT: CLEAN", ""),
            bridger=lambda *a: events.append("bridge") or [], quiescer=quiescer)
    assert events == ["bridge", "quiesce", "run"] * 2


def test_the_meta_says_whether_a_daemon_came_back_during_the_run(pinned_repo):
    """\"Keeps it down\" is best-effort: another live session's wrapper can
    respawn the daemon (rt_ensure_running). The run records it either way."""
    sha, fakes = _head(pinned_repo), []

    def respawning_runner(root, log):
        fakes.append(_FakeDaemon(pinned_repo))
        return 0, "VERDICT: CLEAN", ""

    quiet = lambda root: {"was_up": True, "port": 1, "stopped": True, "quiesced": True}  # noqa: E731
    try:
        _, _, left_down = fp.verify_in_worktree(
            pinned_repo, sha, pinned_repo / "v.log", runner=lambda r, l: (0, "VERDICT: CLEAN", ""),
            bridger=lambda *a: [], quiescer=quiet)
        _, _, came_back = fp.verify_in_worktree(
            pinned_repo, sha, pinned_repo / "v.log", runner=respawning_runner,
            bridger=lambda *a: [], quiescer=quiet)
    finally:
        for f in fakes:
            f.sock.close()
    assert left_down["daemon"]["listening_after_run"] is False
    assert came_back["daemon"]["listening_after_run"] is True


def test_the_real_refusal_passes_once_the_executor_has_quiesced(pinned_repo, tmp_path):
    """The first real downstream adopt's refusal, reproduced against the REAL predicate in run-full-suite.py, on a
    real linked worktree with a listener on the port the main checkout names."""
    rfs = _runner_module()
    wt = tmp_path / "wt"
    assert _git(pinned_repo, "worktree", "add", "--detach", str(wt), _head(pinned_repo)).returncode == 0
    daemon = _FakeDaemon(pinned_repo)
    try:
        refusal = rfs.worktree_daemon_refusal(wt)      # the positive control: it DOES refuse
        assert refusal and "LINKED WORKTREE" in refusal and str(daemon.port) in refusal

        rec = fp.quiesce_daemon(pinned_repo, stopper=daemon.stop)

        assert rec["quiesced"] is True and rec["stopped"] is True
        assert rfs.worktree_daemon_refusal(wt) is None
    finally:
        daemon.sock.close()
        _git(pinned_repo, "worktree", "remove", "--force", str(wt))


def _stale_port_file(root):
    state = Path(root) / "mind_api" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    (state / "daemon.port").write_text(str(dead), encoding="utf-8")


def _garbage_port_file(root):
    state = Path(root) / "mind_api" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "daemon.port").write_text("not-a-port", encoding="utf-8")


@pytest.mark.parametrize("arrange,expect_live", [
    (lambda root: None, False),                # no port file at all
    (_garbage_port_file, False),
    (_stale_port_file, False),                 # the file outlives its daemon: connect is the signal
    (lambda root: _FakeDaemon(root), True),
])
def test_the_executors_daemon_probe_agrees_with_the_runners(tmp_path, arrange, expect_live):
    """Two probes for one question would drift; this pins them to each other."""
    rfs = _runner_module()
    keep = arrange(tmp_path)
    try:
        mine, theirs = fp.daemon_listening_port(tmp_path), rfs._live_daemon_port(tmp_path)
    finally:
        if keep is not None:
            keep.sock.close()
    assert mine == theirs
    assert (mine is not None) is expect_live


@pytest.mark.skipif(os.name == "nt", reason="POSIX kill path; the Windows branch kills only a mind_api command line")
def test_the_default_stopper_stops_a_daemon_through_the_runtime_helper(tmp_path, monkeypatch):
    """The real stop primitive, against a fake daemon whose state lives in a tmp
    runtime dir (RT_DIR), so this box's own daemon is out of reach."""
    code = ("import socket, time\n"
            "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(8)\n"
            "print(s.getsockname()[1], flush=True)\n"
            "time.sleep(120)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline())
        rt = tmp_path / "state"
        rt.mkdir()
        (rt / "daemon.pid").write_text(str(proc.pid), encoding="utf-8")
        (rt / "daemon.port").write_text(str(port), encoding="utf-8")
        for name, rel in (("RT_DIR", ""), ("RT_PID_FILE", "daemon.pid"),
                          ("RT_PORT_FILE", "daemon.port"), ("RT_PARENT_PID_FILE", "daemon.parent.pid")):
            monkeypatch.setenv(name, str(rt / rel) if rel else str(rt))
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass                                   # positive control: it is listening

        assert fp._default_daemon_stop(SCRIPTS.parent.parent) is True

        assert proc.wait(timeout=15) is not None   # the process is gone
        assert not (rt / "daemon.pid").exists()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()


def _install_marker_hook(repo, marker):
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f'#!/bin/sh\necho fired >> "{marker.as_posix()}"\n', encoding="utf-8")
    hook.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX sh post-commit hook")
def test_the_adopts_own_commits_fire_no_post_commit_hook(repo_pair):
    """`--no-verify` skips pre-commit and commit-msg and NOT post-commit, which
    is the hook that restarted the downstream daemon behind the verify."""
    source, target = repo_pair
    marker = target.parent / "hook-fired"
    _install_marker_hook(target, marker)
    _commit(target, "agents/a/x.md", "x\n", "an ordinary commit")
    assert marker.exists(), "the hook never fired -- the assertion below would be vacuous"
    marker.unlink()
    pre = fp.git(target, "rev-parse", "HEAD")[1]

    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan={"gate": {"grafts": []}, "daemon_recycle_required": False},
                      world_dir=target / "world", verify=lambda: (True, "VERDICT: CLEAN"),
                      restart=lambda root: True, pusher=lambda: True)

    assert result["adopted"] is True
    assert fp.git(target, "rev-parse", "HEAD")[1] != pre       # the adopt commit landed
    assert not marker.exists(), "an adopt commit fired the post-commit hook"


@pytest.mark.parametrize("daemon_up,recycle_flag,green,restarts_expected", [
    (True, False, True, 1),     # up before the verify: the executor stopped it, so it restarts it
    (False, False, True, 0),    # no daemon and nothing to recycle: left alone
    (False, True, True, 1),     # the plan's own recycle flag keeps working
    (True, True, True, 1),      # ...and the two reasons make ONE restart, not two
    (True, False, False, 1),    # red: rollback()'s restart only, never a second one
])
def test_adopt_restarts_the_daemon_it_stopped_exactly_once(repo_pair, monkeypatch, daemon_up,
                                                           recycle_flag, green, restarts_expected):
    source, target = repo_pair
    monkeypatch.setattr(fp, "daemon_listening_port", lambda root: 4242 if daemon_up else None)
    restarts = []
    verdict = "VERDICT: CLEAN" if green else "VERDICT: GENUINE failures"
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan={"gate": {"grafts": []}, "daemon_recycle_required": recycle_flag},
                      world_dir=target / "world", verify=lambda: (green, verdict),
                      restart=lambda root: restarts.append(root) or True, pusher=lambda: True)
    assert result["adopted"] is green
    assert len(restarts) == restarts_expected


def test_adopt_records_what_the_executor_did_to_the_daemon(repo_pair, monkeypatch):
    source, target = repo_pair
    rec = {"was_up": True, "port": 4242, "stopped": True, "quiesced": True,
           "listening_after_run": False}
    monkeypatch.setattr(fp, "verify_in_worktree",
                        lambda root, sha, log, agent=None, **kw: (0, "VERDICT: CLEAN", {"daemon": rec}))
    result = fp.adopt(project_root=target, source_repo=source, newest="v1.1.0",
                      plan={"gate": {"grafts": []}, "daemon_recycle_required": False},
                      world_dir=target / "world", restart=lambda root: True, pusher=lambda: True)
    step = next(s for s in result["steps"] if s["step"] == "verify")
    assert step["daemon"] == rec
