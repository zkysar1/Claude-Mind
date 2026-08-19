"""Live-plant round trip: worktree -> seed-transplant -> merge -> iteration-push ().

3/7 of the live-promotion series (1/7 = g-115-4803, the worktree plant path in
promote-to-upstream.sh). Where 1/7 pins Step 4 in isolation, this file walks the
whole loop against a fixture estate and pins the four seams the series depends on
but nothing else exercises together:

  1. seed-transplant.sh's RUNNING gate PASSES on a worktree of a deployment whose
     live checkout is RUNNING -- because a worktree carries no gitignored session
     state. That is what makes a live plant possible at all, and it is stated here
     WITH its negative control, because "the gate did not refuse" and "the gate is
     not there" produce identical output (guard-3534).
  2. The Q4 nothing-to-preserve path: `.env.local` / `.claude/settings.local.json`
     ABSENT at destination. See the ABSENT-CASE note below -- the goal names
     _DEPLOYMENT_LOCAL_FILES, but the seam that owns these two is NOT the one a
     reader would reach for first.
  3. iteration-push.sh consumes the upstream merge into the destination clone.
  4. The post-merge githook fires the daemon recycle on a daemon-surface merge and
     stays quiet on a docs-only one.

HERMETIC, in the three senses the goal names, none of them incidental:
  - fixture repos only: a bare origin, a destination clone, and a merger clone,
    all under tmp_path. The live tree is READ (scripts are copied out of it) and
    never written, and `iteration-push.sh --repo <fixture>` keeps its own
    _paths.sh-derived PROJECT_ROOT out of the picture.
  - STORAGE_BACKEND=local is pinned into every subprocess env (guard-955). The
    conftest already pins it process-wide; this restates it at the subprocess
    boundary because these tests shell out and an inherited own-cloud value makes
    a tmp write collide on a PRODUCTION S3 key (rb-2983).
  - no live daemon hijack: the fixture's `mind-api-start.sh` is a STUB that
    appends to a marker file. The post-merge hook under test is the REAL one and
    it really invokes it -- so the recycle decision is exercised end to end while
    the recycle itself cannot reach this box's daemon.

WHY THE FIXTURE COPIES THE SHIPPED HOOK AND PREDICATE rather than re-typing them
(guard-920): a hand-written stand-in passes forever after the real file rots.
test_the_fixture_carries_the_shipped_hook_and_predicate asserts byte-identity, so
if the copy ever drifts from the shipped file the drift fails a test instead of
quietly making every other test in this file meaningless.

ABSENT-CASE NOTE, and it corrected the goal's own premise (guard-3310). The goal
asks for `_DEPLOYMENT_LOCAL_FILES` preservation "against a worktree where
settings.local.json/.env.local are ABSENT", which reads as a do_plan concern --
do_plan is where deployment-local preservation is reported. It is not. Measured:
those two names are in the manifest's `exclude_always`, so they are never in the
include set, so do_plan's DEPLOYMENT-LOCAL OVERWRITES section can never mention
them in EITHER direction. Verified with a positive control -- writing both files
into a destination and re-running `--plan --living-prod` left the section reading
exactly `.gitignore` + `CLAUDE.md`, unchanged. The seam that actually owns them is
`do_verify_leak_check`, which special-cases them as INFO-when-present so a
destination's own copies are not reported as seed leakage. So the absent case
belongs there, and a test pinning do_plan would have pinned the wrong function
while looking entirely correct.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO = Path(__file__).resolve().parents[2].parent
SCRIPTS = REPO / "core" / "scripts"
SEED_TRANSPLANT = SCRIPTS / "seed-transplant.sh"
ITERATION_PUSH = SCRIPTS / "iteration-push.sh"
WORKTREE_TEARDOWN = SCRIPTS / "worktree-teardown.sh"
SHIPPED_POST_MERGE = REPO / "core" / "githooks" / "post-merge"
SHIPPED_PREDICATE = SCRIPTS / "mind-api-code-changed.sh"

# The exact refusal seed-transplant.sh Step 3c prints, and its exit code. Both
# are asserted so a rename of one without the other cannot pass silently.
RUNNING_REFUSAL = "REFUSE: agent RUNNING"
RUNNING_REFUSAL_RC = 4

# The stub records the args it was called with; the real hook passes --restart.
RESTART_MARKER = ".restart-marker"


def _env(**extra):
    e = dict(os.environ)
    e["STORAGE_BACKEND"] = "local"        # guard-955 -- see module docstring
    e["GIT_TERMINAL_PROMPT"] = "0"        # a credential prompt would hang the suite
    e.update(extra)
    return e


def git(*args, cwd):
    """git with an explicit -C. NEVER `cd` -- see the fixture note below."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True, env=_env(),
    ).stdout.strip()


class Estate:
    """A three-repo fixture estate: bare origin, destination clone, merger clone."""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.dest = root / "dest"
        self.merger = root / "merger"
        self.worktree = root / "plant-wt"

    @property
    def marker(self) -> Path:
        return self.dest / RESTART_MARKER

    def head(self, repo: Path) -> str:
        return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture()
def estate(tmp_path):
    """Bare origin + a destination clone shaped like a downstream deployment.

    The destination is RUNNING (an `agents/<a>/session/agent-state` holding
    RUNNING) and that file is GITIGNORED -- which is the whole point: the
    worktree added from it cannot carry the file, so the RUNNING gate sees a
    clean destination there and a refusing one here.

    Every git call goes through `git -C`. An earlier draft of this fixture used
    `cd` inside a compound shell command; the harness reset cwd mid-sequence and
    the commits landed somewhere other than the intended repo while every command
    still reported success. `-C` removes the ambient dependency entirely.
    """
    e = Estate(tmp_path)

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(e.origin)],
                   check=True, env=_env())
    subprocess.run(["git", "init", "-q", "-b", "main", str(e.dest)],
                   check=True, env=_env())
    git("config", "user.email", "dest@example.invalid", cwd=e.dest)
    git("config", "user.name", "dest", cwd=e.dest)
    git("remote", "add", "origin", str(e.origin), cwd=e.dest)

    for d in ("core/scripts", "core/githooks", "mind_api/src", "docs"):
        (e.dest / d).mkdir(parents=True, exist_ok=True)
    (e.dest / "CLAUDE.md").write_text("deployment-local\n", encoding="utf-8")
    # agents/ ignored so the RUNNING state file is invisible to a worktree;
    # the marker ignored so the stub's output never dirties the tree.
    (e.dest / ".gitignore").write_text(f"agents/\n{RESTART_MARKER}\n", encoding="utf-8")
    (e.dest / "mind_api" / "src" / "__init__.py").write_text("# daemon\n", encoding="utf-8")
    (e.dest / "docs" / "readme.md").write_text("docs\n", encoding="utf-8")

    # The SHIPPED hook + predicate, copied not re-typed (guard-920).
    shutil.copy2(SHIPPED_PREDICATE, e.dest / "core" / "scripts" / SHIPPED_PREDICATE.name)
    shutil.copy2(SHIPPED_POST_MERGE, e.dest / "core" / "githooks" / "post-merge")
    # The ONE stub: the recycle must never reach this box's daemon.
    stub = e.dest / "core" / "scripts" / "mind-api-start.sh"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$*" >> "$(git rev-parse --show-toplevel)/' + RESTART_MARKER + '"\n',
        encoding="utf-8",
    )
    for f in (stub, e.dest / "core" / "githooks" / "post-merge",
              e.dest / "core" / "scripts" / SHIPPED_PREDICATE.name):
        f.chmod(0o755)

    git("add", "-A", cwd=e.dest)
    git("commit", "-qm", "base", cwd=e.dest)
    git("push", "-q", "-u", "origin", "main", cwd=e.dest)
    # core.hooksPath is how the shipped hooks fire in the real estate too
    # (install-git-hooks.sh) -- same mechanism, not a test-only shim.
    git("config", "core.hooksPath", "core/githooks", cwd=e.dest)

    (e.dest / "agents" / "a1" / "session").mkdir(parents=True)
    (e.dest / "agents" / "a1" / "session" / "agent-state").write_text("RUNNING\n", encoding="utf-8")

    git("worktree", "add", "-q", "-b", "plant/v1", str(e.worktree), "main", cwd=e.dest)

    subprocess.run(["git", "clone", "-q", str(e.origin), str(e.merger)], check=True, env=_env())
    git("config", "user.email", "merger@example.invalid", cwd=e.merger)
    git("config", "user.name", "merger", cwd=e.merger)
    return e


def seed_transplant(dest: Path, *flags):
    return subprocess.run(
        [BASH, SEED_TRANSPLANT.as_posix(), str(dest), *flags],
        capture_output=True, text=True, env=_env(), timeout=300,
    )


def upstream_merge(e: Estate, surface: str):
    """Land a merge on origin/main the way a merged PR would.

    `surface` selects WHICH file the plant touches, which is the only variable
    the post-merge hook's decision depends on:
      "daemon" -> mind_api/src/**  (recycle warranted)
      "docs"   -> docs/**          (running daemon is current)
    """
    if surface == "daemon":
        target, body = e.worktree / "mind_api" / "src" / "__init__.py", "# daemon\n# planted\n"
    elif surface == "docs":
        target, body = e.worktree / "docs" / "readme.md", "docs\nmore docs\n"
    else:                                     # never guess a surface silently
        raise AssertionError(f"unknown surface {surface!r}")
    target.write_text(body, encoding="utf-8")
    git("add", "-A", cwd=e.worktree)
    git("commit", "-qm", f"plant: {surface} change", cwd=e.worktree)
    git("push", "-q", "origin", "plant/v1", cwd=e.worktree)

    git("fetch", "-q", "origin", "plant/v1", cwd=e.merger)
    git("merge", "--no-edit", "-q", "FETCH_HEAD", "-m", "merge plant/v1", cwd=e.merger)
    git("push", "-q", "origin", "main", cwd=e.merger)
    return git("rev-parse", "HEAD", cwd=e.merger)


def run_iteration_push(e: Estate):
    """Fetch + integrate into the destination clone. No push: --no-push.

    --fetch-interval-min 0 defeats the FETCH_HEAD-mtime throttle, which would
    otherwise skip the fetch on a fixture whose FETCH_HEAD is seconds old and
    make this test pass or fail on timing. --strict turns the script's
    fail-soft always-0 contract into a real exit code so a genuine merge
    failure cannot read as success.
    """
    return subprocess.run(
        [BASH, ITERATION_PUSH.as_posix(), "--repo", str(e.dest),
         "--no-push", "--strict", "--fetch-interval-min", "0"],
        capture_output=True, text=True, env=_env(), timeout=300,
    )


# --------------------------------------------------------------------------
# Seam 1 -- the RUNNING gate, stated with its negative control.
# --------------------------------------------------------------------------

def test_running_gate_refuses_the_live_checkout(estate):
    """NEGATIVE CONTROL for the test below (guard-3534).

    Without this, a passing worktree test is equally consistent with a gate that
    never fires at all -- and the failure direction is planting over a live
    production deployment, so "the gate is missing" must not be able to
    masquerade as "the gate allowed it."
    """
    p = seed_transplant(estate.dest, "--dry-run")
    combined = p.stdout + p.stderr
    assert RUNNING_REFUSAL in combined, combined[-2000:]
    assert p.returncode == RUNNING_REFUSAL_RC, f"rc={p.returncode}\n{combined[-2000:]}"


def test_running_gate_passes_on_a_worktree_of_the_same_running_deployment(estate):
    """The claim the whole live-promotion series rests on.

    Asserted as "the gate did not refuse", NOT as an exit code: a --dry-run gets
    past Step 3c and into the SOURCE publishability gate, whose verdict depends
    on the state of the live repo this suite happens to be running in. Pinning
    that rc would make this test fail whenever the source has unrelated
    publishability drift -- an assertion about the environment, not the seam
    (guard-3300).
    """
    assert not (estate.worktree / "agents" / "a1" / "session" / "agent-state").exists(), (
        "fixture invariant broken: the worktree carries the gitignored session "
        "state, so this test would prove nothing"
    )
    p = seed_transplant(estate.worktree, "--dry-run")
    combined = p.stdout + p.stderr
    assert RUNNING_REFUSAL not in combined, combined[-2000:]
    assert p.returncode != RUNNING_REFUSAL_RC, combined[-2000:]


# --------------------------------------------------------------------------
# Seam 2 -- Q4: deployment-local files ABSENT at destination.
# --------------------------------------------------------------------------

def _seed_engine():
    sys.path.insert(0, str(SCRIPTS))
    import _seed_engine
    return _seed_engine


def _manifest():
    import yaml
    return yaml.safe_load((REPO / "core" / "config" / "seed-manifest.yaml").read_text(encoding="utf-8"))


def test_absent_deployment_local_files_are_neither_leaks_nor_info(estate):
    """Q4, on the seam that actually owns it (see the ABSENT-CASE note above).

    `.env.local` and `.claude/settings.local.json` are excluded from the seed so
    they are never COPIED, and special-cased at verify so a destination's OWN
    copies are not reported as leakage. With neither present there is nothing to
    preserve and nothing to report -- the correct outcome is silence in BOTH
    directions, and the direction that matters is `leaked`: a false leak here
    would fail a promotion over a file that legitimately does not exist yet.
    """
    E = _seed_engine()
    for rel in (".env.local", ".claude/settings.local.json"):
        assert not (estate.worktree / rel).exists(), f"fixture already carries {rel}"

    result = E.do_verify_leak_check(estate.worktree, _manifest())
    for rel in (".env.local", ".claude/settings.local.json"):
        assert rel not in result["leaked"], result
        assert rel not in result["info"], result


def test_present_deployment_local_files_are_reported_as_info_not_leaks(estate):
    """POSITIVE CONTROL for the test above (guard-2421).

    An empty `leaked` list is the pass condition there, and an empty list is also
    what a check that never inspects these paths returns. This proves the check
    can see them: with the files PRESENT they must appear -- as info, never as
    leaks.
    """
    E = _seed_engine()
    (estate.worktree / ".claude").mkdir(parents=True, exist_ok=True)
    (estate.worktree / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    (estate.worktree / ".env.local").write_text("KEY=value\n", encoding="utf-8")

    result = E.do_verify_leak_check(estate.worktree, _manifest())
    for rel in (".env.local", ".claude/settings.local.json"):
        assert rel in result["info"], result
        assert rel not in result["leaked"], result


# --------------------------------------------------------------------------
# Seams 3 + 4 -- the round trip, and the recycle decision at the end of it.
# --------------------------------------------------------------------------

def test_iteration_push_consumes_the_upstream_merge(estate):
    merged = upstream_merge(estate, "daemon")
    assert estate.head(estate.dest) != merged, "fixture invariant: dest must start behind"

    p = run_iteration_push(estate)
    combined = p.stdout + p.stderr
    assert p.returncode == 0, combined[-2000:]
    assert estate.head(estate.dest) == merged, combined[-2000:]
    # Content, not just the ref: a ref move with the wrong tree is still a bug.
    assert "planted" in (estate.dest / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8")


def test_post_merge_hook_fires_on_a_daemon_surface_merge(estate):
    """The merge moved mind_api/src/**, so the running daemon is stale."""
    assert not estate.marker.exists()
    upstream_merge(estate, "daemon")
    run_iteration_push(estate)
    assert estate.marker.exists(), "post-merge hook did not recycle on a daemon-surface merge"
    assert "--restart" in estate.marker.read_text(encoding="utf-8"), (
        "plain mind-api-start.sh is health-only idempotent -- the hook must pass --restart"
    )


def test_post_merge_hook_stays_quiet_on_a_docs_only_merge(estate):
    """NEGATIVE CONTROL for the test above.

    The predicate fails TOWARD restart on any git error, so a hook that recycled
    unconditionally -- or a predicate that errored on every input -- would pass
    the positive test. Only this one separates "decided correctly" from "always
    says yes."
    """
    merged = upstream_merge(estate, "docs")
    run_iteration_push(estate)
    assert estate.head(estate.dest) == merged, "the docs merge must still be consumed"
    assert not estate.marker.exists(), (
        "post-merge recycled the daemon for a docs-only merge -- pure churn"
    )


def test_worktree_teardown_removes_the_plant_worktree(estate):
    """--owner is the load-bearing flag: the worktree belongs to the DESTINATION
    repo, not to the repo the script is invoked from. Without it the teardown
    resolves against PROJECT_ROOT, git reports "not a working tree", and the
    registration is left behind to wedge the next plant (g-115-4803)."""
    assert estate.worktree.is_dir()
    p = subprocess.run(
        [BASH, WORKTREE_TEARDOWN.as_posix(), "--owner", str(estate.dest), str(estate.worktree)],
        capture_output=True, text=True, env=_env(), timeout=120,
    )
    combined = p.stdout + p.stderr
    assert p.returncode == 0, combined[-2000:]
    assert not estate.worktree.exists(), combined[-2000:]
    # The registration, not just the directory: a stale registration is what
    # actually wedges the next run.
    assert str(estate.worktree) not in git("worktree", "list", cwd=estate.dest)


# --------------------------------------------------------------------------
# The fixture's own integrity.
# --------------------------------------------------------------------------

def test_the_fixture_carries_the_shipped_hook_and_predicate(estate):
    """guard-920: the estate must exercise the REAL files, not stand-ins.

    Every hook assertion in this file is worthless if the copy drifts from what
    ships, and drift is silent -- the stand-in keeps passing. This turns that
    into a failing test. The ONE deliberate exception is mind-api-start.sh, which
    is a stub by design; it is asserted to be a stub so the exception can never
    quietly become an accident.
    """
    for shipped, rel in ((SHIPPED_POST_MERGE, "core/githooks/post-merge"),
                         (SHIPPED_PREDICATE, f"core/scripts/{SHIPPED_PREDICATE.name}")):
        assert (estate.dest / rel).read_bytes() == shipped.read_bytes(), rel

    stub = (estate.dest / "core" / "scripts" / "mind-api-start.sh").read_text(encoding="utf-8")
    assert RESTART_MARKER in stub and "python" not in stub, (
        "the daemon-start stub grew a real implementation -- this suite would "
        "recycle the box's live daemon"
    )
