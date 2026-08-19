""" — the worker carrier must reach origin even when the merge DEFERS.

The defect: `iteration-push.sh --push-worker-ref` performed its push in a block
placed BELOW the fetch/integrate step, and every integrate-deferral seam called
`soft_exit` before reaching it. Because `soft_exit` returns 0 without --strict,
the caller saw rc=0 while the ref on origin never moved — the Body's commits
were stranded silently.

Measured live 2026-08-16 (alpha worker Body, cc-07): a 1-line diff in
`agents/zeta/aspirations.jsonl` — a partner store file the Body never touched,
left dirty as ordinary own-cloud read-through-cache background state — deferred
the merge and stranded commit 99cb344c2.

These tests use a hermetic bare-repo "origin" + clone, so nothing touches the
live tree or the real remote.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "iteration-push.sh"

# guard-580/581: never a bare "bash" argv[0] (resolves to System32 WSL on win32
# and can hang forever), and never str(Path) for the script path (bash silently
# strips a WindowsPath's backslashes). BASH + .as_posix() is the sanctioned pair.
sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
from _bash_helpers import BASH  # noqa: E402

AGENT = "testagent"
SID = "testsid-0000"
WREF = f"refs/workers/{AGENT}/{SID}"


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


def _env():
    env = os.environ.copy()
    # guard-955: never let a test inherit own-cloud — an S3 key is derived from
    # the filename, not the tmp path, so a tmp write would hit the real store.
    env["STORAGE_BACKEND"] = "local"
    env["MIND_AGENT"] = AGENT
    env["MIND_SID"] = SID
    return env


@pytest.fixture
def deferring_clone(tmp_path):
    """A clone whose merge from origin is guaranteed to DEFER.

    Shape: origin is one commit ahead on `core/shared.txt`, and the clone has an
    uncommitted local change to that same file, so `git merge` refuses before
    starting (MERGE_HEAD absent -> the dirty-defer seam).

    `core/shared.txt` is deliberately NOT under `agents/<other>/` — the
    cross-agent-churn self-heal (g-115-1843) would clear that shape and the
    merge would succeed, which is a different path than the one under test.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    other = tmp_path / "other"

    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)

    subprocess.run(["git", "clone", str(origin), str(work)],
                   capture_output=True, check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "core").mkdir(parents=True, exist_ok=True)
    (work / "core" / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "-u", "origin", "main")

    # A second clone pushes an origin-ahead commit touching the same file.
    subprocess.run(["git", "clone", str(origin), str(other)],
                   capture_output=True, check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    (other / "core" / "shared.txt").write_text("base\nfrom-origin\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "origin-ahead")
    _git(other, "push", "origin", "main")

    # The worker's OWN local commit — this is what must reach the reducer.
    (work / "core" / "worker-edit.txt").write_text("worker framework edit\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "worker local commit")

    # Dirty the contended file so the merge refuses before starting.
    (work / "core" / "shared.txt").write_text("base\nLOCALLY DIRTY\n", encoding="utf-8")

    return {"origin": origin, "work": work}


def _run_push(work):
    return subprocess.run(
        [BASH, SCRIPT.as_posix(), "--repo", str(work), "--push-worker-ref",
         "--worker-ref-agent", AGENT, "--worker-ref-sid", SID,
         "--fetch-interval-min", "0"],
        capture_output=True, text=True, env=_env(),
    )


def test_merge_actually_defers_in_this_fixture(deferring_clone):
    """Positive control: the fixture really does produce a deferral.

    Without this, a passing carrier test could be passing because the merge
    SUCCEEDED, which would silently stop exercising the defect's path.
    """
    work = deferring_clone["work"]
    res = _run_push(work)
    combined = res.stdout + res.stderr
    assert "DEFERRED" in combined or "blocked on" in combined, (
        "fixture did not produce a merge deferral — the test would be vacuous.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_worker_ref_reaches_origin_despite_deferred_merge(deferring_clone):
    """THE REGRESSION PIN. Pre-fix this fails: the ref is absent on origin."""
    origin, work = deferring_clone["origin"], deferring_clone["work"]
    head = _git(work, "rev-parse", "HEAD").stdout.strip()

    res = _run_push(work)

    ls = _git(origin, "for-each-ref", "--format=%(objectname) %(refname)", WREF).stdout.strip()
    assert ls, (
        f"worker ref {WREF} is ABSENT on origin after --push-worker-ref.\n"
        "This is the g-115-6368 defect: the merge deferred and the push block "
        "was never reached, while the script exited "
        f"{res.returncode}.\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert ls.split()[0] == head, (
        f"worker ref points at {ls.split()[0]}, expected HEAD {head}"
    )


def test_deferral_still_reported_and_not_silently_converted_to_success(deferring_clone):
    """Flushing the carrier must NOT mask the deferral.

    The fix routes the deferral seams through a helper that pushes and then
    exits on the deferral path. The deferral must still be logged, so a real
    integration wedge stays visible.
    """
    work = deferring_clone["work"]
    res = _run_push(work)
    combined = res.stdout + res.stderr
    assert "pushed HEAD" in combined, "carrier push was not reported"
    assert "DEFERRED" in combined or "blocked on" in combined, (
        "the deferral disappeared from the output — a real integration wedge "
        "would now be invisible"
    )


def test_clean_merge_path_still_pushes(tmp_path):
    """The clean-integrate path must be unchanged by the refactor."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(origin), str(work)],
                   capture_output=True, check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "-u", "origin", "main")
    (work / "g.txt").write_text("y\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "worker commit")

    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    _run_push(work)
    ls = _git(origin, "for-each-ref", "--format=%(objectname)", WREF).stdout.strip()
    assert ls == head, f"clean path regressed: ref={ls!r} head={head}"


def test_unresolved_identity_still_refuses(tmp_path):
    """Identity guard survives the extraction — a ref missing a segment would
    collide across bodies, the one property this carrier guarantees."""
    # A real origin is required: the script exits at the "no origin/main ref"
    # check well before the identity guard, so a bare `git init` here would
    # make this test vacuous (measured — it passed the wrong assertion first).
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(origin), str(work)],
                   capture_output=True, check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "-u", "origin", "main")

    env = _env()
    env.pop("MIND_AGENT", None)
    env.pop("MIND_SID", None)
    res = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--repo", str(work), "--push-worker-ref",
         "--worker-ref-agent", "", "--worker-ref-sid", "",
         "--fetch-interval-min", "0"],
        capture_output=True, text=True, env=env,
    )
    assert "REFUSED" in (res.stdout + res.stderr), (
        f"identity guard lost.\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_every_deferral_seam_routes_through_the_carrier_helper():
    """Structural pin (the goal's check 2): a future deferral seam that calls
    `soft_exit` directly would strand the carrier again exactly as before.

    This is a source-shape assertion on purpose — the behavioural tests above
    cover the seams that exist TODAY, and this one covers the seam somebody
    adds tomorrow.
    """
    src = SCRIPT.read_text(encoding="utf-8").splitlines()
    offenders = []
    for i, line in enumerate(src):
        if "_ip_defer_streak_tick" not in line:
            continue
        # the exit belongs on the next non-blank, non-comment line
        for nxt in src[i + 1:i + 4]:
            s = nxt.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("soft_exit"):
                offenders.append((i + 1, line.strip(), s))
            break
    assert not offenders, textwrap.dedent(f"""
        A deferral seam exits via soft_exit instead of _ip_defer_exit, so the
        worker carrier is stranded on that path (g-115-6368):
        {offenders}
    """)


# --------------------------------------------------------------------------
# check-outputs delivery verification ( verification outcome 2).
#
# The table-only answer reported `carried` for a commit that never left the
# box. These pin the three verdicts of the delivery check itself. They
# monkeypatch PROJECT_ROOT so nothing touches the live tree or real origin.
# --------------------------------------------------------------------------

import worker_execute as we  # noqa: E402


def _mk_pushed_clone(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(origin), str(work)],
                   capture_output=True, check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "-u", "origin", "main")
    return origin, work


def test_delivery_verified_when_ref_equals_head(tmp_path, monkeypatch):
    _origin, work = _mk_pushed_clone(tmp_path)
    _git(work, "push", "origin", f"HEAD:{WREF}")
    monkeypatch.setattr(we, "PROJECT_ROOT", work)
    verdict, detail = we.git_ref_delivery(["framework-file-edit"], agent=AGENT, sid=SID)
    assert verdict == "verified", (verdict, detail)


def test_delivery_stranded_when_head_moved_past_the_ref(tmp_path, monkeypatch):
    """THE  SHAPE: a channel exists, the ref exists, and the commit
    still did not land. Table-only checking reports `carried` here."""
    _origin, work = _mk_pushed_clone(tmp_path)
    _git(work, "push", "origin", f"HEAD:{WREF}")
    (work / "later.txt").write_text("later work\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "unpushed worker commit")
    monkeypatch.setattr(we, "PROJECT_ROOT", work)
    verdict, detail = we.git_ref_delivery(["local-git-commit"], agent=AGENT, sid=SID)
    assert verdict == "stranded", (verdict, detail)
    assert "local-git-commit" in detail


def test_delivery_stranded_when_ref_absent(tmp_path, monkeypatch):
    _origin, work = _mk_pushed_clone(tmp_path)
    monkeypatch.setattr(we, "PROJECT_ROOT", work)
    verdict, detail = we.git_ref_delivery(["framework-file-edit"], agent=AGENT, sid=SID)
    assert verdict == "stranded" and "ABSENT" in detail, (verdict, detail)


def test_delivery_na_for_non_git_classes(tmp_path, monkeypatch):
    """Classes carried by the storage backend must not be gated on a git push."""
    _origin, work = _mk_pushed_clone(tmp_path)
    monkeypatch.setattr(we, "PROJECT_ROOT", work)
    verdict, _ = we.git_ref_delivery(["goal-record", "working-memory"], agent=AGENT, sid=SID)
    assert verdict == "n/a", verdict


def test_delivery_unverified_never_reports_verified(tmp_path, monkeypatch):
    """An unrunnable check is ignorance, not an all-clear — the distinction the
    whole fix rests on."""
    _origin, work = _mk_pushed_clone(tmp_path)
    monkeypatch.setattr(we, "PROJECT_ROOT", work)
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.delenv("MIND_SID", raising=False)
    monkeypatch.setattr(we, "AGENT_NAME", None)
    verdict, detail = we.git_ref_delivery(["framework-file-edit"], agent=None, sid=None)
    assert verdict == "unverified", (verdict, detail)
    assert verdict != "verified"
