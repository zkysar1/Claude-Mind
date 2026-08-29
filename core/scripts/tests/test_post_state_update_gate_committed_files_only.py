"""test_post_state_update_gate_committed_files_only.py — regression test for .

Exercises the COMMIT_SHA-scoped detection branch added to
core/scripts/post-state-update-gate.sh (Option B from the g-115-1154
investigation). When iteration-close.sh extracts the commit_sha from
iteration-commit.sh's JSON output and exports COMMIT_SHA, the gate scopes its
fresh-eyes file-detection to exactly the files THAT COMMIT landed
(git diff --name-only ${SHA}~1..${SHA}) and skips untracked detection, instead
of re-deriving from a working tree that may carry partner WIP at neutral paths
(core/scripts/, core/config/, mind_api/src/) — the stranded-partner
false-positive class.

Cases (acceptance criteria a/b/c from the goal):
  1. committed scope (COMMIT_SHA valid) → gate fires on exactly the committed
     core files; an UNCOMMITTED partner file in the working tree is NOT counted.
     Also asserts the guard-343 new-script trigger survives committed scope.
     (criterion b — gate does NOT fire on this-agent-only committed set noise,
      and criterion a — the working-tree partner file that WOULD have
      over-counted pre-fix is excluded.)
  2. COMMIT_SHA unset → working-tree behavior unchanged: the gate sees the
     UNCOMMITTED working-tree files, NOT the (clean-vs-HEAD) committed set.
     (criterion c — backward-compat.)
  3. COMMIT_SHA set but invalid (not a resolvable commit) → falls back to
     working-tree behavior identical to case 2. (criterion c — invalid path.)

The fixture builds a fixed contrast: 3 self core files COMMITTED (one a new .py)
and 3 partner core files left UNCOMMITTED in the working tree. Committed scope
and working-tree scope therefore detect DISJOINT sets, which is exactly the
scoping switch under test.

Pattern: subprocess + project-local tempdir + scripted git init, invoking the
REAL gate by absolute path with cwd=<temp repo> so its siblings (_paths.sh,
helpers) resolve against the real repo while `git` operates on the fixture.
Mirrors test_iteration_commit_untracked_filter.py. World/meta are redirected to
empty temp dirs (MIND_WORLD/MIND_META, honored by _paths.sh) so the gate's
cooldown peer-read + attribution filter are deterministic. The autouse
fixture targets an OFF-ROSTER test agent, so no live fleet member's
fresh_eyes_last_fire WM slot is read or written at all (g-115-4887).
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

# Project-local tempdir; the OS tempdir under C:\Users\... is unreachable from
# this environment's bash (mountpoint mismatch). Mirrors the sibling
# iteration-commit tests.
PROJECT_TMP = SCRIPT_DIR / "_tmp_committed_scope_test"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402

# Synthetic fixture paths. Committed (this-agent) vs uncommitted (partner WIP).
COMMITTED = [
    "core/scripts/cfo-committed-a.sh",
    "core/scripts/cfo-committed-b.sh",
    "core/scripts/cfo-committed-new.py",  # new .py — guard-343 new-script trigger
]
PARTNER_UNCOMMITTED = [
    "core/scripts/cfo-partner-x.py",
    "core/scripts/cfo-partner-y.py",
    "core/scripts/cfo-partner-z.py",
]


# OFF-ROSTER by construction: must never appear in the live team-state
# agent_status roster ( outcome 1). Was "zeta", a LIVE fleet
# member used as a stand-in for "some agent" (guard-1699).
AGENT = "testagent"


def _to_bash_path(p) -> str:
    """Convert a Windows path to Git-Bash mount-prefix form (/c/...)."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=15
    )
    return out.stdout.strip()


def _setup_repo(tmp: Path) -> tuple[Path, str]:
    """Init a git repo, commit 3 self core files, leave 3 partner files
    uncommitted. Returns (repo, committed_sha)."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # core/scripts/ tracked via .gitkeep so new files appear as file paths, not
    # a collapsed untracked-dir entry.
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "core" / "scripts" / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    # The goal's commit: 3 self core files (one a new .py).
    for rel in COMMITTED:
        (repo / rel).write_text(f"# {rel}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "goal commit"], cwd=repo, check=True)
    sha = _git(repo, "rev-parse", "HEAD")

    # Partner WIP left UNCOMMITTED in the working tree (untracked).
    for rel in PARTNER_UNCOMMITTED:
        (repo / rel).write_text(f"# {rel}\n")
    return repo, sha


def _run_gate(repo: Path, world: Path, meta: Path, commit_sha: str | None,
              goal_id: str | None = None) -> dict:
    """Run the REAL gate with cwd=repo; return parsed JSON output."""
    env = dict(os.environ)
    env["MIND_AGENT"] = AGENT            # off-roster test agent — never a live member
    env["MIND_WORLD"] = _to_bash_path(world)  # empty temp world → deterministic filter/peer-read
    env["MIND_META"] = _to_bash_path(meta)
    env.pop("COMMIT_SHA", None)
    env.pop("GOAL_ID", None)
    if commit_sha is not None:
        env["COMMIT_SHA"] = commit_sha
    if goal_id is not None:
        env["GOAL_ID"] = goal_id
    result = subprocess.run(
        [GIT_BASH, _to_bash_path(GATE_SH), "deep"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The gate prints a single JSON line on stdout. Parse the last line that is
    # itself valid JSON (robust against any leading diagnostics).
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


@pytest.fixture(autouse=True)
def _isolate_wm_cooldown():
    """Point the gate's cooldown read at an OFF-ROSTER test agent, so the suite
    never touches a live fleet member's working memory (g-115-4887).

    THIS FILE IS THE ORIGIN OF THE PATTERN -- mode_only and daemon_dispatch each
    copied the fixture from here, live agent name and all. g-115-4887 named only
    those two; this third instance was found by grepping the fixture shape, which
    is exactly what that goal's own SCOPE section told the executor to do.

    The old rationale for using a LIVE agent was that "the daemon does NOT honor
    MIND_AGENT_DIR -- it refuses to resolve a non-existent test agent
    ('WORLD_PATH unresolved'), so a dedicated-test-agent isolation is infeasible
    with the live daemon (guard-672)". MEASURED 2026-08-29 (alpha, cc-08): that
    constraint is retired. _paths.sh falls through to the first available conf
    for WORLD/META while AGENT_DIR still points at the NAMED agent (g-115-960 /
    g-115-6417), so `MIND_AGENT=testagent wm-read.sh fresh_eyes_last_fire
    --json` returns `{}` on a real, off-roster agent dir. Positive control: the
    same call for `alpha` returns live records, so the `{}` is a genuine empty.

    The save/restore is DELETED rather than hardened -- a test agent's scratch
    slot has nothing worth preserving, and the old shape swallowed every failure
    (`except Exception: pass`, no `finally`), so a crash between neutralize and
    restore destroyed the real list permanently and silently. The neutralize is
    kept for determinism and now ASSERTS rather than swallowing.
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


def test_committed_scope_detects_only_committed_files():
    """Case 1: COMMIT_SHA valid → fires on exactly the committed core files;
    the uncommitted partner files are NOT counted. guard-343 new-script trigger
    survives committed scope."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo, sha = _setup_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        out = _run_gate(repo, world, meta, commit_sha=sha)

        assert out["fired"] is True, f"expected fire on committed core files: {out}"
        assert out["core_count"] == 3, f"expected exactly 3 committed core files: {out}"
        files = set(out.get("files", []))
        assert files == set(COMMITTED), f"committed scope must list exactly committed files: {files}"
        for pf in PARTNER_UNCOMMITTED:
            assert pf not in files, f"uncommitted partner file leaked into committed scope: {pf} in {files}"
        # guard-343 new-script trigger preserved: a committed-added script is reported.
        assert out.get("new_script") in COMMITTED, \
            f"new-script trigger lost under committed scope: new_script={out.get('new_script')!r}"


def test_unset_commit_sha_uses_working_tree():
    """Case 2 (backward-compat): COMMIT_SHA unset → working-tree scope. The gate
    sees the uncommitted working-tree files, NOT the clean-vs-HEAD committed set."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo, _sha = _setup_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        out = _run_gate(repo, world, meta, commit_sha=None)

        assert out["fired"] is True, f"expected fire on working-tree files: {out}"
        files = set(out.get("files", []))
        assert files == set(PARTNER_UNCOMMITTED), \
            f"working-tree scope must list the uncommitted files: {files}"
        for cf in COMMITTED:
            assert cf not in files, \
                f"committed (clean-vs-HEAD) file must NOT appear in working-tree scope: {cf} in {files}"


def test_invalid_commit_sha_falls_back_to_working_tree():
    """Case 3 (backward-compat): COMMIT_SHA set but unresolvable → falls back to
    working-tree behavior identical to case 2."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo, _sha = _setup_repo(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        out = _run_gate(repo, world, meta, commit_sha="not-a-real-sha-deadbeef")

        assert out["fired"] is True, f"expected fire (fallback to working tree): {out}"
        files = set(out.get("files", []))
        assert files == set(PARTNER_UNCOMMITTED), \
            f"invalid COMMIT_SHA must fall back to working-tree set: {files}"
        for cf in COMMITTED:
            assert cf not in files, \
                f"committed file must NOT appear under invalid-sha fallback: {cf} in {files}"


def _setup_repo_multicommit(tmp: Path) -> tuple[Path, str]:
    """Two goal commits stamped '()': a mid-Phase-4 CODE commit (3
    core files, one new .py) followed by a close-time DOCS-ONLY commit. Returns
    (repo, close_sha) — the docs commit is what iteration-close passes as
    COMMIT_SHA (the g-115-2026 leak shape)."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "core" / "scripts").mkdir(parents=True)
    (repo / "core" / "scripts" / ".gitkeep").write_text("")
    (repo / "agents" / "zeta").mkdir(parents=True)
    (repo / "agents" / "zeta" / "journal.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    # Mid-Phase-4 code commit (e.g. committed early for a daemon restart).
    for rel in COMMITTED:
        (repo / rel).write_text(f"# {rel}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fix(g-115-9999): wire the thing"], cwd=repo, check=True)

    # Close-time commit: docs/state only — zero core files.
    (repo / "agents" / "zeta" / "journal.md").write_text("init\ngoal closed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "docs(g-115-9999): journal + state"], cwd=repo, check=True)
    close_sha = _git(repo, "rev-parse", "HEAD")
    return repo, close_sha


def test_goal_id_unions_midgoal_commits():
    """: GOAL_ID unions every '(goal-id)'-stamped commit into the
    committed scope, so a mid-Phase-4 code commit is detected even when the
    close-time COMMIT_SHA carries only docs."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo, close_sha = _setup_repo_multicommit(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        out = _run_gate(repo, world, meta, commit_sha=close_sha, goal_id="g-115-9999")

        assert out["fired"] is True, f"expected fire via goal-commit union: {out}"
        assert out.get("commits_scanned") == 2, \
            f"expected both goal commits scanned: {out}"
        files = set(out.get("files", []))
        assert set(COMMITTED) <= files, \
            f"mid-goal code commit's files missing from union: {files}"
        assert out.get("new_script") in COMMITTED, \
            f"new-script trigger lost in multi-commit union: {out.get('new_script')!r}"


def test_goal_id_absent_preserves_single_sha_leak_shape():
    """Backward-compat pin: WITHOUT GOAL_ID the docs-only close commit stays
    below thresholds (the documented pre-g-115-2030 limitation — this assert
    is the regression contract for the single-sha path, not an endorsement)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo, close_sha = _setup_repo_multicommit(tmp)
        world = tmp / "world"; world.mkdir()
        meta = tmp / "meta"; meta.mkdir()

        out = _run_gate(repo, world, meta, commit_sha=close_sha, goal_id=None)

        assert out["fired"] is False, \
            f"single-sha docs-only commit must not fire (below thresholds): {out}"
        assert out.get("commits_scanned") == 1, f"expected exactly the close sha: {out}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
