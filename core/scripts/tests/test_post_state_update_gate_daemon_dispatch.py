"""test_post_state_update_gate_daemon_dispatch.py — regression test for .

`core/scripts/post-state-update-gate.sh` built its dispatch set with
`grep '^core/'`, so a path under `mind_api/src/` could NEVER appear in the
emitted `files[]`, by construction. A deep goal whose entire production change
lived in the daemon therefore dispatched an adversarial review of its TESTS
ONLY — coverage the code never got. Canonical: g-306-141 fired core_count=2,
loc_changed=298 naming two test files while the production change was in
mind_api/src/endpoints/aspirations_write.py, and the single real finding that
review produced was in the unreviewed file.

The fix widens the DISPATCH SET ONLY. The TRIGGER thresholds deliberately did
NOT widen, and case 2 below PINS that decision so a future widening has to be a
deliberate test change rather than an accident. Basis (measured over the
trailing 600 commits, 2026-08-04): 66 commits touch mind_api/src; 64 (97%)
already fire and merely cannot name the daemon file; only 2 fire nothing at
all, and those 2 are one change plus its merge commit — so counting mind_api/src
toward the thresholds would newly fire ~1 distinct change per 600 commits (0.3%)
at the price of contradicting guard-343's published spec, whose thresholds are
defined in terms of "core/ files" and "LOC delta in core/scripts".

Cases:
  1. test_daemon_file_rides_along_in_dispatch_list — a firing core-file change
     set that also touches mind_api/src lists the daemon file in files[] while
     core_count still counts ONLY the core files.
  2. test_daemon_only_change_does_not_fire         — a change set of daemon
     files alone stays below threshold and does NOT fire. Pins the recorded
     scope decision (set widened, trigger not).
  3. test_core_only_dispatch_unchanged             — the goal's own check 2:
     a core/scripts-only change set is byte-for-byte unaffected by the fix.

Pattern mirrors test_post_state_update_gate_mode_only.py: subprocess + a
project-local tempdir + scripted git init, invoking the REAL gate by absolute
path with cwd=<temp repo>. World/meta are redirected to empty temp dirs so the
attribution filter + cooldown peer-read are deterministic; the autouse fixture
neutralizes then restores the test agent's fresh_eyes_last_fire WM slot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_SH = CORE_SCRIPTS / "post-state-update-gate.sh"
WM_READ_SH = CORE_SCRIPTS / "wm-read.sh"
WM_SET_SH = CORE_SCRIPTS / "wm-set.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_daemon_dispatch_test"
# OFF-ROSTER by construction: must never appear in the live team-state
# agent_status roster ( outcome 1). Was "zeta" -- a LIVE fleet
# member -- copied here from mode_only during  (guard-1699).
AGENT = "testagent"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _to_bash_path(p) -> str:
    """Convert a Windows path to Git-Bash mount-prefix form (/c/...); no-op on POSIX."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=15
    )
    return out.stdout.strip()


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "core" / "scripts" / ".gitkeep").write_text("")
    (repo / "mind_api" / "src" / "endpoints").mkdir(parents=True)
    (repo / "mind_api" / "src" / "endpoints" / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _run_gate(repo: Path, world: Path, meta: Path, commit_sha: str | None) -> dict:
    env = dict(os.environ)
    env["MIND_AGENT"] = AGENT
    env["MIND_WORLD"] = _to_bash_path(world)
    env["MIND_META"] = _to_bash_path(meta)
    env.pop("COMMIT_SHA", None)
    if commit_sha is not None:
        env["COMMIT_SHA"] = commit_sha
    result = subprocess.run(
        [GIT_BASH, _to_bash_path(GATE_SH), "deep"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
    )
    parsed = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
    assert parsed is not None, (
        f"gate produced no JSON. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return parsed


def _commit_files(repo: Path, rels, body: str, msg: str) -> None:
    for rel in rels:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)


@pytest.fixture(autouse=True)
def _isolate_wm_cooldown():
    """Point the gate's cooldown read at an OFF-ROSTER test agent, so the suite
    never touches a live fleet member's working memory (g-115-4887).

    This file INHERITED the hazard by copying the fixture out of
    test_post_state_update_gate_mode_only.py during g-306-149 -- live agent name
    included. That is the mechanism worth remembering: the pattern spread by
    example, from a file that looked correct. See _isolate_wm_cooldown there for
    the full rationale, the measurement that retired the guard-672 constraint,
    and why the save/restore half was deleted rather than hardened.
    """
    r = subprocess.run(
        [GIT_BASH, _to_bash_path(WM_SET_SH), "fresh_eyes_last_fire"],
        input="null", capture_output=True, text=True, timeout=30,
        env={**os.environ, "MIND_AGENT": AGENT},
    )
    assert r.returncode == 0, (
        f"could not neutralize {AGENT} fresh_eyes_last_fire "
        f"(rc={r.returncode} stderr={r.stderr!r}) -- the firing cases below would "
        f"be non-deterministic, so fail loudly rather than flake"
    )
    yield


DAEMON_REL = "mind_api/src/endpoints/aspirations_write.py"


def test_daemon_file_rides_along_in_dispatch_list():
    """The whole point of : a firing change set that ALSO touches
    mind_api/src must name the daemon file in files[], while core_count keeps
    counting only the core files. Both halves are asserted — naming it is the
    fix, and NOT counting it is the scope decision."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        core_rels = [f"core/scripts/t-{i}.sh" for i in range(3)]
        _commit_files(repo, core_rels + [DAEMON_REL], "base\n", "base")
        _commit_files(repo, core_rels + [DAEMON_REL], "base\nedited\n", "goal")
        sha = _git(repo, "rev-parse", "HEAD")

        out = _run_gate(repo, world, meta, sha)

        assert out["fired"] is True, out
        assert DAEMON_REL in out["files"], (
            f"daemon production file missing from dispatch list — this is the "
            f"g-306-141 defect. files={out['files']}"
        )
        for rel in core_rels:
            assert rel in out["files"], out
        # The trigger arithmetic must NOT have absorbed the daemon file.
        assert out["core_count"] == len(core_rels), (
            f"core_count absorbed the daemon file — the SET widened but the "
            f"TRIGGER must not have. out={out}"
        )


def test_daemon_only_change_does_not_fire():
    """PINS THE SCOPE DECISION. A change set of daemon files ALONE stays below
    threshold and does not fire, because mind_api/src is deliberately excluded
    from CORE_FILE_THRESHOLD and LOC_THRESHOLD. If a future change widens the
    trigger too, this test must be updated deliberately — that is its job."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        daemon_rels = [
            "mind_api/src/endpoints/aspirations_write.py",
            "mind_api/src/endpoints/claim.py",
            "mind_api/src/endpoints/release.py",
            "mind_api/src/endpoints/coordination.py",
        ]
        _commit_files(repo, daemon_rels, "base\n", "base")
        _commit_files(repo, daemon_rels, "base\nedited\n", "goal")
        sha = _git(repo, "rev-parse", "HEAD")

        out = _run_gate(repo, world, meta, sha)

        # 4 daemon files would clear CORE_FILE_THRESHOLD=3 if they were counted.
        assert out["fired"] is False, (
            f"daemon-only change fired — the TRIGGER widened, which g-306-149 "
            f"decided against on measured grounds. out={out}"
        )
        assert out["core_count"] == 0, out
        # A non-firing gate emits no files[] at all.
        assert "files" not in out, out


def test_core_only_dispatch_unchanged():
    """The goal's own verification check 2: a core/scripts-only change set is
    unaffected by the fix — same fire, same count, and no stray extras in the
    dispatch list."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _init_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        core_rels = [f"core/scripts/c-{i}.sh" for i in range(3)]
        _commit_files(repo, core_rels, "base\n", "base")
        _commit_files(repo, core_rels, "base\nedited\n", "goal")
        sha = _git(repo, "rev-parse", "HEAD")

        out = _run_gate(repo, world, meta, sha)

        assert out["fired"] is True, out
        assert out["core_count"] == len(core_rels), out
        assert sorted(out["files"]) == sorted(core_rels), (
            f"a core-only change set gained files it did not have before. "
            f"out={out}"
        )
