#!/usr/bin/env python3
"""Tests for core/scripts/framework_pull.py ().

Covers the four things the goal names -- fetch, compare, preflight-gate,
rollback -- plus the pure decision logic each one turns on. The fixture-repo
tests build REAL git repos so the plan path is exercised end to end rather
than mocked; the rollback test performs a real reset and asserts the tree
came back, because the goal requires that path be exercised, not documented.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
