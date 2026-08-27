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
    out = fp.rollback(target, before, restart=lambda root: calls.append(root) or True)

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
