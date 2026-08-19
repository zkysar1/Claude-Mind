#!/usr/bin/env python3
"""Dogfood fixtures for core/scripts/commit-reachability.py (gap-128 forge, Step 3.6).

FIXTURE SEAM — WHAT THESE COVER AND WHAT THEY EXCLUDE (guard-1462).
The fixtures build REAL throwaway git repos with a REAL `origin` remote and
drive the script's PUBLIC entry point (`triage`). Nothing is injected below
the API, so ref enumeration, the mirror-prefix subtraction, the ancestry
call and the classification are ALL exercised — not just the interpreter.

EXCLUDED, and named rather than implied:
  * network transport — `origin` is a local bare repo, so a fetch that fails
    for auth/DNS reasons is not reachable here. Covered instead by the LIVE
    run recorded in the SKILL.md verification section.
  * the `--no-fetch` CLI plumbing and argparse layer (the tests call `triage`
    directly). `test_cli_smoke` covers the argv path shallowly.

ANTI-VACUITY: every verdict below is asserted per-fixture, and
`test_verdicts_are_distinct` proves the probe DISCRIMINATES rather than
returning one constant. Per guard-1793 the aggregate is a supplement to the
per-fixture assertions, never a substitute — mutate against the per-fixture
assertions, not against the summary line.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_mod = __import__("importlib").import_module("importlib.util")
_spec = _mod.spec_from_file_location(
    "commit_reachability", SCRIPT_DIR / "commit-reachability.py"
)
cr = _mod.module_from_spec(_spec)
_spec.loader.exec_module(cr)


def _run(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr}"
    return p.stdout.strip()


def _commit(repo, name):
    (Path(repo) / name).write_text(name)
    _run(repo, "add", name)
    _run(repo, "commit", "-q", "-m", name)
    return _run(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def world():
    """One repo carrying every reachability shape at once.

    Built once because the shapes are independent and the classification is a
    pure read — sharing the repo keeps the suite fast without coupling cases.
    """
    tmp = tempfile.mkdtemp(prefix="reach-fixture-")
    origin = Path(tmp) / "origin.git"
    work = Path(tmp) / "work"

    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _run(work, "config", "user.email", "fixture@example.invalid")
    _run(work, "config", "user.name", "fixture")
    _run(work, "remote", "add", "origin", str(origin))

    shas = {}
    shas["landed"] = _commit(work, "base.txt")
    _run(work, "push", "-q", "origin", "main")

    # (a) worker-ref-only commit — pushed ONLY to refs/workers/**
    _run(work, "checkout", "-q", "-b", "wk", shas["landed"])
    shas["worker"] = _commit(work, "worker-only.txt")
    _run(work, "push", "-q", "origin", "wk:refs/workers/fixture/abc123-1")

    # (b) remote-branch-only commit — a normal unmerged feature branch
    _run(work, "checkout", "-q", "-b", "feat", shas["landed"])
    shas["branch"] = _commit(work, "feature-only.txt")
    _run(work, "push", "-q", "origin", "feat")

    # (c) local-only commit — committed, never pushed anywhere
    _run(work, "checkout", "-q", "-b", "localonly", shas["landed"])
    shas["local"] = _commit(work, "local-only.txt")

    # (d) dangling commit — reachable from no ref at all
    _run(work, "checkout", "-q", "-b", "doomed", shas["landed"])
    shas["dangling"] = _commit(work, "doomed.txt")
    _run(work, "checkout", "-q", "main")
    _run(work, "branch", "-q", "-D", "doomed")

    # Drop the local branches that would otherwise ALSO contain (a) and (b),
    # so each fixture isolates the namespace it is testing.
    _run(work, "branch", "-q", "-D", "wk")
    _run(work, "branch", "-q", "-D", "feat")
    _run(work, "fetch", "-q", "origin")

    return {"work": str(work), "shas": shas}


def _triage(world, key, **kw):
    return cr.triage(repo=world["work"], sha=world["shas"][key],
                     target_ref="origin/main", worker_namespace="workers", **kw)


def test_landed(world):
    r = _triage(world, "landed")
    assert r["verdict"] == cr.LANDED
    assert r["landed"] is True


def test_worker_ref_stranded(world):
    """The branch every neighbouring tool is blind to — the reason this exists."""
    r = _triage(world, "worker")
    assert r["verdict"] == cr.STRANDED_WORKER_REF
    assert r["landed"] is False
    assert r["containing_refs"]["worker"], "worker refs must be reported"


def test_worker_ref_is_not_miscounted_as_a_remote_branch(world):
    """Discriminating pair: the mirror lives UNDER refs/remotes/.

    Without the mirror-prefix subtraction, a worker-only commit also matches
    `refs/remotes/` and would classify as STRANDED_REMOTE_BRANCH — the wrong
    landing path (merge a PR that does not exist). Deleting the subtraction
    line kills this test.
    """
    r = _triage(world, "worker")
    assert r["containing_refs"]["remote_branches"] == []
    assert r["verdict"] != cr.STRANDED_REMOTE_BRANCH


def test_remote_branch_stranded(world):
    r = _triage(world, "branch")
    assert r["verdict"] == cr.STRANDED_REMOTE_BRANCH
    assert r["containing_refs"]["worker"] == []
    assert any("feat" in x for x in r["containing_refs"]["remote_branches"])


def test_local_only_stranded(world):
    r = _triage(world, "local")
    assert r["verdict"] == cr.STRANDED_LOCAL_ONLY
    assert r["containing_refs"]["remote_branches"] == []


def test_dangling_is_absent(world):
    r = _triage(world, "dangling")
    assert r["verdict"] == cr.ABSENT
    assert r["containing_refs"] == {
        "worker": [], "remote_branches": [], "local_branches": []
    }


def test_unknown_sha_is_inconclusive_not_absent(world):
    """rc=128 is NOT an answer.

    An unfetched sha and a nonexistent one are indistinguishable from here.
    Collapsing them into ABSENT is the defect /is-change-live's Restricted
    Operations #2 documents; this asserts we do not.
    """
    r = cr.triage(repo=world["work"], sha="0" * 40, target_ref="origin/main",
                  do_fetch=False)
    assert r["verdict"] == cr.INCONCLUSIVE
    assert r["landed"] is None
    assert "retry" in r["reason"]


def test_missing_target_ref_is_inconclusive_never_landed(world):
    """The fail-safe direction, asserted directly.

    An unreadable ancestry test must never produce LANDED — that is the
    verdict that ends an investigation (guard-3398).
    """
    r = cr.triage(repo=world["work"], sha=world["shas"]["landed"],
                  target_ref="origin/no-such-ref", do_fetch=False)
    assert r["verdict"] == cr.INCONCLUSIVE
    assert r["landed"] is not True


def test_not_a_git_repo_is_inconclusive():
    with tempfile.TemporaryDirectory() as d:
        r = cr.triage(repo=d, sha="0" * 40, do_fetch=False)
        assert r["verdict"] == cr.INCONCLUSIVE
        assert "not a git repository" in r["reason"]


def test_every_verdict_carries_a_landing_path():
    """A verdict with no route is a verdict the caller cannot act on."""
    for v in (cr.LANDED, cr.STRANDED_WORKER_REF, cr.STRANDED_REMOTE_BRANCH,
              cr.STRANDED_LOCAL_ONLY, cr.ABSENT, cr.INCONCLUSIVE):
        assert cr.LANDING_PATH[v].strip()


def test_verdicts_are_distinct(world):
    """Anti-vacuity SUPPLEMENT — not the guard. See the module docstring."""
    got = {
        _triage(world, "landed")["verdict"],
        _triage(world, "worker")["verdict"],
        _triage(world, "branch")["verdict"],
        _triage(world, "local")["verdict"],
        _triage(world, "dangling")["verdict"],
    }
    assert len(got) == 5, f"probe lacks discriminating power: {got}"


def test_cli_smoke(world):
    """Shallow argv-layer cover — the fixtures above call triage() directly."""
    p = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "commit-reachability.py"),
         "--sha", world["shas"]["landed"], "--repo", world["work"],
         "--target-ref", "origin/main", "--no-fetch"],
        capture_output=True, text=True, timeout=120,
    )
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["verdict"] == cr.LANDED
