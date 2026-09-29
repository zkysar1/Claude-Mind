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
  7. THE BASIS (g-115-11144): after the loop's own refresh (`git fetch origin
     main`, which brings no tags) a box that did not cut the newest tag reads
     the previous one as newest. measure_with_basis, the CLI, the nudge and the
     probe must all see origin's newest tag, and a failed refresh leaves a DUE
     reading unmeasured.
  8. Leases: only a strictly older tag the basis can see is superseded; the
     latest HAND close of a tag holds re-filing for skip_hold_hours.
  9. --nudge never reads the goal store when the train is not due.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime
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

    def refresh(self) -> None:
        """The basis refresh: the only writer of the release-train tag namespace."""
        why = rt.refresh_basis(self.work)
        assert why is None, why


@pytest.fixture
def repo(tmp_path):
    """A clone whose basis was refreshed once, at v1.0.0."""
    r = Repo(tmp_path)
    r.commit("core/scripts/a.py", "base")
    r.tag("v1.0.0")
    r.push()
    r.refresh()
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
    assert set(got) == {"stale_hours", "ticks_to_file", "ticks_to_revalidate", "skip_hold_hours"}
    for key, value in declared.items():
        assert got[key] == value, f"{key}: read {got[key]}, config declares {value}"


def test_skip_hold_is_shorter_than_eviction_age():
    """last_disposal() reads the LIVE store, which evicts a terminal goal after
    aspirations_eviction.age_days. A hold longer than that would end early and
    silently, the day the closed lease it reads is evicted."""
    import yaml
    with (CORE_SCRIPTS.parent / "config" / "aspirations.yaml").open(encoding="utf-8") as f:
        age_days = (yaml.safe_load(f) or {})["aspirations_eviction"]["age_days"]
    assert 0 < rt.config()["skip_hold_hours"] < age_days * 24


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
    # A higher tag that origin/main does not contain must not count, even with
    # origin carrying it and the refresh bringing it.
    _git(repo.work, "checkout", "-b", "unmerged")
    repo.commit("core/scripts/e.py", "e")
    repo.tag("v9.0.0")
    _git(repo.work, "push", "--quiet", "origin", "v9.0.0")
    _git(repo.work, "checkout", "main")
    repo.refresh()
    assert f"{rt.TAG_NAMESPACE}/v9.0.0" in _git(repo.work, "for-each-ref", "--format=%(refname)",
                                                  rt.TAG_NAMESPACE)
    m = rt.measure(repo.work, PATHS, now=OLD_TS)
    assert m["newest_tag"] == "v1.10.0"
    assert m["commits_past"] == 0


def test_measure_reports_errors_instead_of_raising(tmp_path):
    no_tag_error = f"no v* tag in {rt.TAG_NAMESPACE} is reachable from origin/main"
    r = Repo(tmp_path)
    r.commit("core/scripts/a.py", "untagged")
    no_origin = rt.measure(r.work, PATHS)
    assert no_origin["error"] and no_origin["newest_tag"] is None
    r.push()
    r.refresh()
    assert rt.measure(r.work, PATHS)["error"] == no_tag_error
    # refs/tags is never read: a tag there that no refresh has brought reads as
    # no tag at all, so measure_with_basis refreshes before acting.
    r.tag("v1.0.0")
    r.push()
    assert _git(r.work, "tag", "-l") == "v1.0.0"
    assert rt.measure(r.work, PATHS)["error"] == no_tag_error
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


# ── 7. the basis () ───────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _cut_elsewhere(repo: Repo, tmp_path: Path, tag: str) -> None:
    """Another box commits framework code, cuts `tag` on it NOW, and pushes
    both. This clone then refreshes the way the loop does (`git fetch origin
    main`), which brings the commit and not the tag."""
    other = tmp_path / "cutter"
    _git(tmp_path, "clone", "--quiet", "--branch", "main", str(repo.origin), str(other))
    for k, v in (("user.email", "c@example.invalid"), ("user.name", "c"),
                 ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
                 ("core.hooksPath", str(tmp_path / "no-hooks")), ("core.autocrlf", "false")):
        _git(other, "config", k, v)
    (other / "core" / "scripts").mkdir(parents=True, exist_ok=True)
    (other / "core" / "scripts" / "cut.py").write_text(f"{tag}\n", encoding="utf-8")
    _git(other, "add", "core/scripts/cut.py")
    _git(other, "commit", "-m", f"framework change released as {tag}")
    _git(other, "tag", "-a", tag, "-m", tag, date=_now_iso())
    _git(other, "push", "--quiet", "origin", "main", tag)
    _git(repo.work, "fetch", "--quiet", "origin", "main")
    assert _git(repo.work, "tag", "-l", tag) == "", (
        f"precondition: this git's `git fetch origin main` brought {tag}, so the "
        f"defect pinned here cannot occur on it (measured absent on git 2.45)")


def test_a_tagless_fetch_hides_the_new_tag_and_measure_with_basis_finds_it(repo, tmp_path):
    _cut_elsewhere(repo, tmp_path, "v1.1.0")
    local = rt.measure(repo.work, PATHS)
    assert local["newest_tag"] == "v1.0.0"              # the previous tag, read as newest
    assert rt.decide(local, 24)["due"] is True          # ... so a FALSE stall
    m = rt.measure_with_basis(repo.work, PATHS, 24)
    assert m["error"] is None and m["tag_basis"] == "fetched"
    assert m["newest_tag"] == "v1.1.0" and m["tags_merged"][:2] == ["v1.1.0", "v1.0.0"]
    assert rt.decide(m, 24)["due"] is False


def test_cli_and_nudge_see_the_true_newest_tag_after_a_tagless_fetch(repo, tmp_path):
    """Against f6208f77ed the CLI read DUE on v1.0.0, and the nudge printed an
    LLM-ACTION for the stale v1.0.0 lease."""
    _cut_elsewhere(repo, tmp_path, "v1.1.0")
    world = _world(tmp_path, goals=[
        {"id": "g-115-77777", "status": "pending", "origin_signal": rt.signal_for("v1.0.0")}])
    rc, out, _ = _cli(repo.work, world)
    assert rc == 0 and out.startswith("release-train: ok - no framework commits past v1.1.0"), out
    # That run fetched the tag. Drop it again, so the nudge starts from a basis
    # without it and must fetch for itself.
    ref = f"{rt.TAG_NAMESPACE}/v1.1.0"
    _git(repo.work, "update-ref", "-d", ref)
    rc, out, _ = _cli(repo.work, world, "--nudge")
    assert rc == 0 and out == ""
    assert _git(repo.work, "for-each-ref", "--format=%(refname)", ref) == ref
    assert _git(repo.work, "tag", "-l", "v1.1.0") == ""      # refs/tags is never written


def test_a_tag_cut_here_and_never_pushed_is_not_origins_newest(repo):
    """, replayed on real git: this box cuts v1.1.0 in its OWN
    refs/tags and pushes main WITHOUT it. Origin's newest is still v1.0.0, but
    read from refs/tags the local-only tag was named newest, and the v1.0.0
    lease read as superseded by a tag no other box can see."""
    repo.commit("core/scripts/late.py", "framework change")
    repo.tag("v1.1.0", date=_now_iso())
    _git(repo.work, "push", "--quiet", "origin", "main")        # main only, no tag
    assert _git(repo.origin, "tag", "-l", "v1.1.0") == ""
    m = rt.measure_with_basis(repo.work, PATHS, 24, always=True)
    assert m["error"] is None and m["tag_basis"] == "fetched"
    assert m["newest_tag"] == "v1.0.0" and m["tags_merged"] == ["v1.0.0"]
    lease = {"id": "g-origin-newest", "origin_signal": rt.signal_for("v1.0.0")}
    assert rt.superseded_leases([lease], m["tags_merged"]) == []
    assert _git(repo.work, "tag", "-l", "v1.1.0") == "v1.1.0"   # the local cut is untouched


def test_a_local_tag_that_differs_from_origins_does_not_stop_the_measurement(repo):
    """A local v1.0.0 that is not origin's (re-cut here, or a cut half done).
    Fetched into refs/tags without '+', git refused the clobber, and --quiet
    hid the refusal: the reading stayed on the local tag, whose fresh date read
    a stalled train as not due. The namespace refresh measures origin's tag and
    leaves the local one as it was."""
    repo.commit("core/scripts/late.py", "framework change past the old tag")
    repo.push()                                          # origin: v1.0.0 old + 1 commit = due
    _git(repo.work, "tag", "-d", "v1.0.0")
    _git(repo.work, "tag", "-a", "v1.0.0", "-m", "not origin's", date=_now_iso())
    m = rt.measure_with_basis(repo.work, PATHS, 24, always=True)
    assert m["error"] is None and m["tag_basis"] == "fetched"
    assert m["newest_tag"] == "v1.0.0" and m["tag_created"] == OLD   # origin's tag
    assert rt.decide(m, 24)["due"] is True
    assert _git(repo.work, "for-each-ref", "--format=%(contents:subject)",
                "refs/tags/v1.0.0") == "not origin's"


def test_a_refused_refresh_names_the_ref_git_refused(repo, tmp_path):
    """--quiet made git print nothing when it refused a ref, so the recorded
    reason was a bare rc. Git's per-ref status line names the ref and why."""
    _cut_elsewhere(repo, tmp_path, "v1.1.0")
    lock = repo.work / ".git" / Path(f"{rt.TAG_NAMESPACE}/v1.1.0.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")                # a concurrent writer holds the ref
    why = rt.refresh_basis(repo.work)
    assert why and f"v1.1.0 -> {rt.TAG_NAMESPACE}/v1.1.0" in why, why
    m = rt.measure_with_basis(repo.work, PATHS, 24)
    assert m["tag_basis"].startswith("local (refresh failed") and f"{rt.TAG_NAMESPACE}/v1.1.0" in m["error"]
    assert rt.decide(m, 24)["reason"].startswith("unmeasured:")


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_release_train_basis", CORE_SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_on_a_box_that_did_not_cut_the_tag_files_and_retires_nothing(
        repo, tmp_path, monkeypatch):
    """The fleet-level harm, end to end on real git. Against f6208f77ed this box
    retired the cutter's open v1.1.0 lease as "a newer tag, v1.0.0, has been
    cut" and filed a false v1.0.0 lease."""
    _cut_elsewhere(repo, tmp_path, "v1.1.0")
    wd = _load_watchdog()
    lease = {"id": "g-cut", "status": "pending", "claimed_by": None,
             "origin_signal": rt.signal_for("v1.1.0"), "_source": "world"}
    monkeypatch.setattr(rt, "self_role", lambda world: "frontier")
    monkeypatch.setattr(rt, "framework_paths", lambda: PATHS)
    monkeypatch.setattr(rt, "open_release_goals", lambda world, agent_dir=None: [lease])
    monkeypatch.setattr(rt, "last_disposal", lambda *a, **k: None, raising=False)
    ctx = types.SimpleNamespace(agent_name="t", project_root_path=repo.work,
                                agent_dir=tmp_path / "agent")
    p = wd.ReleaseTrainProbe(ctx)
    calls = {"file": [], "retire": []}
    monkeypatch.setattr(p, "_file_release_goal",
                        lambda *a, **k: calls["file"].append(a) or {"filed": True, "goal_id": "g-x"})
    monkeypatch.setattr(p, "_retire_release_goals",
                        lambda goals, reason: calls["retire"].append(
                            ([g["id"] for g in goals], reason)) or {"closed": []})
    monkeypatch.setattr(p, "_post_board_alert", lambda *a, **k: {"posted": True})
    assert p.check() == []
    assert calls == {"file": [], "retire": []}, calls
    assert "v1.1.0" in p.last_reason


def test_a_failed_basis_refresh_leaves_a_due_reading_unmeasured(repo, tmp_path, monkeypatch):
    repo.commit("core/scripts/late.py", "framework change past the old tag")
    repo.push()                                          # local: v1.0.0 old + 1 commit = due
    _git(repo.work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    m = rt.measure_with_basis(repo.work, PATHS, 24)
    assert m["tag_basis"].startswith("local (refresh failed")
    assert "basis refresh failed" in m["error"] and "v1.0.0" in m["error"]
    assert rt.decide(m, 24)["reason"].startswith("unmeasured:")
    # The deliberate opt-out (guard-4582) fails the same safe way.
    _git(repo.work, "remote", "set-url", "origin", str(repo.origin))
    monkeypatch.setenv(rt.NO_FETCH_ENV, "1")
    m = rt.measure_with_basis(repo.work, PATHS, 24)
    assert rt.NO_FETCH_ENV in m["error"]


def test_a_local_not_due_reading_needs_no_fetch(tmp_path):
    r = Repo(tmp_path)
    r.commit("core/scripts/a.py", "base")
    r.tag("v2.0.0", date=_now_iso())
    r.commit("core/scripts/b.py", "past a fresh tag")
    r.push()
    r.refresh()
    _git(r.work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # any fetch fails
    m = rt.measure_with_basis(r.work, PATHS, 24)
    assert m["error"] is None and m["tag_basis"] == "local" and m["newest_tag"] == "v2.0.0"
    forced = rt.measure_with_basis(r.work, PATHS, 24, always=True)   # a reader re-measuring
    assert forced["error"] is None and forced["tag_basis"].startswith("local (refresh failed")


# ── 8. leases ────────────────────────────────────────────────────────────────

def test_superseded_leases_are_strictly_older_and_visible():
    merged = ["v1.10.0", "v1.9.0", "v1.2.0"]
    goals = [{"id": "same", "origin_signal": rt.signal_for("v1.10.0")},
             {"id": "older", "origin_signal": rt.signal_for("v1.9.0")},
             {"id": "unseen-newer", "origin_signal": rt.signal_for("v1.11.0")},
             {"id": "unseen-older", "origin_signal": rt.signal_for("v1.3.0")},
             {"id": "other", "origin_signal": "investigate:git-drift-detected-x"},
             {"id": "bare"}]
    assert [g["id"] for g in rt.superseded_leases(goals, merged)] == ["older"]
    assert rt.superseded_leases(goals, []) == []


def test_last_disposal_is_the_latest_hand_close(tmp_path):
    sig = rt.signal_for("v1.0.0")
    world = _world(tmp_path, goals=[
        {"id": "g-open", "status": "pending", "origin_signal": sig},
        {"id": "g-early", "status": "completed", "origin_signal": sig,
         "completed_at": "2026-09-20T10:00:00", "outcome_note": "decided: no release needed"},
        {"id": "g-hand", "status": "skipped", "origin_signal": sig,
         "completed_at": "2026-09-26T10:00:00",
         "outcome_note": "not yet: the widget refactor must soak first"},
        {"id": "g-probe", "status": "skipped", "origin_signal": sig,
         "completed_at": "2026-09-27T10:00:00",
         "outcome_note": f"{rt.PROBE_RETIRE_MARK}: a newer tag, v1.1.0, has been cut"},
        {"id": "g-other-tag", "status": "skipped", "origin_signal": rt.signal_for("v0.9.0"),
         "completed_at": "2026-09-27T11:00:00", "outcome_note": "another tag"},
    ])
    # A line that is not an aspiration object must not raise (the --nudge contract).
    with (world / "aspirations.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps([sig]) + "\n")
    got = rt.last_disposal(world, None, sig)
    assert got["id"] == "g-hand" and got["_source"] == "world"
    assert [g["id"] for g in rt.open_release_goals(world)] == ["g-open"]
    assert rt.last_disposal(world, None, rt.signal_for("v9.9.9")) is None
    assert rt.last_disposal(tmp_path / "absent", None, sig) is None


def test_held_until_holds_only_inside_the_window():
    now = datetime(2026, 9, 27, 12, 0, 0)
    prior = {"completed_at": "2026-09-27T02:00:00"}
    assert rt.held_until(prior, 24, now=now) == "2026-09-28T02:00:00"
    assert rt.held_until({"completed_at": "2026-09-26T11:00:00"}, 24, now=now) is None
    assert rt.held_until(prior, 0, now=now) is None
    assert rt.held_until(None, 24, now=now) is None
    # An undatable close cannot hold the detector quiet.
    assert rt.held_until({"completed_at": "garbled"}, 24, now=now) is None
    assert rt.held_until({}, 24, now=now) is None
    # A zoned stamp is read on the fleet's UTC clock.
    assert rt.held_until({"completed_at": "2026-09-27T04:00:00+02:00"}, 24,
                         now=now) == "2026-09-28T02:00:00"


# ── 9. --nudge short-circuit ─────────────────────────────────────────────────

def _load_cli():
    spec = importlib.util.spec_from_file_location("release_train_check_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo_at(root: Path, tag_date: str) -> Repo:
    root.mkdir()
    r = Repo(root)
    r.commit("core/scripts/a.py", "base")
    r.tag("v2.0.0", date=tag_date)
    r.commit("core/scripts/b.py", "framework change past the tag")
    r.push()
    return r


def test_nudge_does_not_read_the_goal_store_when_not_due(tmp_path, monkeypatch):
    reads = []
    monkeypatch.setattr(rt, "open_release_goals", lambda *a, **k: reads.append(a) or [])
    cli = _load_cli()
    world = _world(tmp_path, goals=[])
    fresh = _repo_at(tmp_path / "fresh", _now_iso())
    assert cli.main(["--nudge", "--repo", str(fresh.work), "--world-dir", str(world)]) == 0
    assert reads == []
    # Positive control: when due, the store IS read, so the stub can see a read.
    due = _repo_at(tmp_path / "due", OLD)
    assert cli.main(["--nudge", "--repo", str(due.work), "--world-dir", str(world)]) == 0
    assert len(reads) == 1
