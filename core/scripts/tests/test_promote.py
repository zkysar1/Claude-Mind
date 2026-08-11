"""test_promote.py — Wave 2 promotion-wrapper coverage (daemon-safe).

Two layers, mirroring test_release.py / test_check_upstream.py:

  1. Pure-lib + CLI tests for promotion_allowed (CW4 — exactly one downstream
     step) via the check-promotion-order subcommand.
  2. Black-box subprocess tests of promote-to-upstream.sh in SAFE modes only:
       - arg/usage handling (exit 2),
       - --dry-run against tmp target clones (NO seed-transplant, NO PR, NO
         mutation of the target or this repo — the dry-run stops before any
         write), exercising the frontier-invariant (CW2) accept/reject paths and
         the malformed-target guards,
       - the end-of-chain NOTARGET refusal (CW4) via an MIND_WORLD-redirected
         self_role=downstream overlay.
     The tests NEVER plant, NEVER open a PR, and NEVER mutate the real repo.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _release_lib as L  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

PROMOTE_SH = CORE_SCRIPTS / "promote-to-upstream.sh"
LIB = CORE_SCRIPTS / "_release_lib.py"
INIT_PY = PROJECT_ROOT / "mind_api" / "src" / "__init__.py"
CHAIN = "frontier,seed,downstream"


def _local_version() -> str:
    for line in INIT_PY.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise AssertionError("no __version__ in real __init__.py")


def run_promote(*args, world=None, extra_env=None):
    env = os.environ.copy()
    if world is not None:
        env["MIND_WORLD"] = str(world)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(PROMOTE_SH), *args],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )


def lib_order(chain: str, frm: str, to: str):
    return subprocess.run(
        [sys.executable, str(LIB), "check-promotion-order", chain, frm, to],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )


def _mk_target(tmp_path: Path, version: str) -> Path:
    tgt = tmp_path / "tgt"
    (tgt / "mind_api" / "src").mkdir(parents=True)
    (tgt / "mind_api" / "src" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    return tgt


# ===========================================================================
# 1. Pure-lib: promotion_allowed (CW4 — exactly one downstream step)
# ===========================================================================
@pytest.mark.parametrize("frm,to,ok", [
    ("frontier", "seed", True),
    ("seed", "downstream", True),
    ("frontier", "downstream", False),  # skip — not a single step
    ("downstream", "seed", False),      # backwards
    ("seed", "frontier", False),        # backwards
    ("frontier", "frontier", False),    # self
    ("bogus", "seed", False),           # unknown from
    ("seed", "bogus", False),           # unknown to
])
def test_promotion_allowed(frm, to, ok):
    assert L.promotion_allowed(["frontier", "seed", "downstream"], frm, to) is ok


# ===========================================================================
# 2. check-promotion-order CLI (OK exit 0 / DENY exit 1)
# ===========================================================================
def test_cli_order_frontier_to_seed_ok():
    r = lib_order(CHAIN, "frontier", "seed")
    assert r.returncode == 0 and "OK" in r.stdout


def test_cli_order_seed_to_downstream_ok():
    r = lib_order(CHAIN, "seed", "downstream")
    assert r.returncode == 0 and "OK" in r.stdout


def test_cli_order_frontier_to_downstream_deny():
    r = lib_order(CHAIN, "frontier", "downstream")
    assert r.returncode == 1 and "DENY" in r.stdout


def test_cli_order_downstream_to_seed_deny():
    r = lib_order(CHAIN, "downstream", "seed")
    assert r.returncode == 1 and "DENY" in r.stdout


def test_cli_order_unknown_role_deny():
    r = lib_order(CHAIN, "seed", "ghost")
    assert r.returncode == 1 and "DENY" in r.stdout


# ===========================================================================
# 3. promote-to-upstream.sh — usage / arg handling (exit 2)
# ===========================================================================
def test_shell_missing_target_exit2():
    r = run_promote("--dry-run")
    assert r.returncode == 2 and "--target" in r.stderr


def test_shell_target_no_value_exit2():
    r = run_promote("--target", "--dry-run")
    assert r.returncode == 2


def test_shell_branch_no_value_exit2(tmp_path):
    tgt = _mk_target(tmp_path, "0.1.0")
    r = run_promote("--target", str(tgt), "--branch", "--dry-run")
    assert r.returncode == 2


def test_shell_unknown_arg_exit2():
    r = run_promote("--bogus")
    assert r.returncode == 2


def test_shell_help_exit0():
    r = run_promote("--help")
    assert r.returncode == 0


# ===========================================================================
# 4. promote-to-upstream.sh — target guards + frontier-invariant (CW2), dry-run
# ===========================================================================
def test_shell_target_not_a_dir_exit1(tmp_path):
    missing = tmp_path / "does-not-exist"
    r = run_promote("--target", str(missing), "--dry-run")
    assert r.returncode == 1


def _release_chain_divergent() -> bool:
    """True when this box's RELEASES.json newest entry != __version__ — the
    promote preflight refuses with 'RELEASES.json not current' BEFORE reaching
    the error paths the two live-repo shell tests below assert (unsynced
    satellite box, merge deferred; g-115-1940). Fail-open on read errors."""
    try:
        nv = L.newest_version(L.load_releases(str(PROJECT_ROOT / "RELEASES.json")))
        txt = (PROJECT_ROOT / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8")
        ver = next((ln.split('"')[1] for ln in txt.splitlines()
                    if ln.startswith("__version__") and '"' in ln), None)
        return bool(nv and ver and nv != ver)
    except Exception:
        return False


_live_release_chain_synced = pytest.mark.skipif(
    _release_chain_divergent(),
    reason="release chain anchor divergent on this box (RELEASES.json newest "
           "!= __version__) — promote preflight fails before the tested "
           "behavior; sync/merge the box to re-enable (g-115-1940)",
)


@_live_release_chain_synced
def test_shell_target_missing_init_exit1(tmp_path):
    bare = tmp_path / "bare"; bare.mkdir()
    r = run_promote("--target", str(bare), "--dry-run")
    assert r.returncode == 1
    assert "__init__.py" in r.stderr


@_live_release_chain_synced
def test_shell_invariant_violation_higher_target_exit1(tmp_path):
    """CW2: cannot promote BACKWARDS — target ahead of local is refused."""
    tgt = _mk_target(tmp_path, "99.0.0")
    r = run_promote("--target", str(tgt), "--dry-run")
    assert r.returncode == 1
    assert "INVARIANT VIOLATION" in r.stderr


# NOTE (): the dry-run HAPPY-PATH tests (lower/equal target -> "[dry-run]
# OK") moved to section 7 and now run against an ISOLATED clean+tagged source with
# a stubbed seed-preflight. run_promote() targets the LIVE repo, and promote Step 3
# invokes seed-preflight UNCONDITIONALLY (dry-run softens only the Step-1b/1c
# release-ceremony notes, NOT content publishability -- intentional since commit
# c92777b1). So the happy path depended on the live committed repo being fully
# publishable; any committed publishability defect (e.g. a domain-token leak in a
# core/config design doc) failed both tests with seed-preflight exit 1, in CI as
# well as dev -- and they were the suite's two slowest tests (~140s each, running
# the real seed-preflight). The FAILURE-path dry-run tests above legitimately use
# the live repo: they exit 1 BEFORE Step 3, so publishability never enters.


# ===========================================================================
# 5. promote-to-upstream.sh — end-of-chain NOTARGET refusal (CW4)
# ===========================================================================
def test_shell_end_of_chain_notarget_exit1(tmp_path):
    """A repo whose self_role is the LAST chain link has nothing downstream to
    promote to — refused (CW4). Redirect the overlay via MIND_WORLD."""
    world = tmp_path / "world"
    (world / "config").mkdir(parents=True)
    (world / "config" / "compatibility.yaml").write_text(
        "self_role: downstream\n", encoding="utf-8")
    tgt = _mk_target(tmp_path, "0.1.0")
    r = run_promote("--target", str(tgt), "--dry-run", world=world)
    assert r.returncode == 1
    assert "end of the chain" in r.stderr


# ===========================================================================
# 6. promote-to-upstream.sh — --pr path with local bare remote (omni-fu1 / F3)
# ===========================================================================
# The real --pr write path (Steps 4/4b/5/6) NEVER ran in a test before this. A
# real-repo run is unusable here: Step 1 requires a CLEAN tree AND HEAD == the
# v$LOCAL tag commit (false during development). So we build an ISOLATED source
# repo (clean + tagged) and stub the three heavy sub-steps:
#   * seed-preflight.sh / seed-verify.sh -> exit 0 (publishability is tested
#     elsewhere; here the unit-under-test is promote's --pr ORDERING + push + gh).
#   * seed-transplant.sh -> cp the source __init__.py into the target + commit
#     onto the CURRENT branch. This faithfully reproduces the ONE property F3
#     guards: the plant commits onto whatever branch is checked out. If Step 4
#     (branch BEFORE plant) regressed, the commit would land on main and the PR
#     diff would be empty (an M2 auto-merge) — exactly what these tests catch.
# The target gets a local bare remote as origin; gh is a PATH-shim that records
# its args. Nothing touches the real repo, GitHub, or a real gh.
from test_release import _git, requires_git, _GIT  # noqa: E402  (git harness reuse)

_FW_CHAIN_YAML = "promotion_chain:\n  - role: frontier\n  - role: seed\n  - role: downstream\n"
_STUB_OK = "#!/usr/bin/env bash\nexit 0\n"
# Stub plant: cp source __init__.py into target + commit onto the CURRENT branch.
_STUB_TRANSPLANT = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'TARGET="$1"\n'
    'SDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
    'mkdir -p "$TARGET/mind_api/src"\n'
    'cp "$SDIR/../../mind_api/src/__init__.py" "$TARGET/mind_api/src/__init__.py"\n'
    'echo planted > "$TARGET/PLANTED.txt"\n'
    'git -C "$TARGET" add -A\n'
    'git -C "$TARGET" commit -q -m "chore: sync framework"\n'
)


def _gitcfg(repo: Path) -> None:
    _git(repo, "config", "user.email", "promote-test@example.com")
    _git(repo, "config", "user.name", "Promote Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")


def _setup_promote_source(tmp_path: Path, version: str = "1.0.0", frontier: bool = True) -> Path:
    """Isolated, clean source repo carrying promote + its libs + stubbed sub-steps.
    promote anchors PROJECT_ROOT to <src>/core/scripts/../..

    frontier=True  -> a frontier source: ships RELEASES.json + a v<version> tag
                      (the release-provenance a frontier cut produces).
    frontier=False -> a SEED source (g-115-1811): NO RELEASES.json, NO tag — the
                      un-bootstrapped-seed condition (claude-mind). promote's
                      release-provenance gates (Step 1a RELEASES.json + Step 1c
                      v-tag) must SKIP for it (role-conditional, option 2)."""
    src = tmp_path / "src"
    (src / "core" / "scripts").mkdir(parents=True)
    (src / "core" / "config").mkdir(parents=True)
    (src / "mind_api" / "src").mkdir(parents=True)
    # check-releases-current.sh is REQUIRED here: promote Step 1a delegates the
    # RELEASES.json check to it (, single role-aware checker).
    # promotion-preflight.{sh,py} are REQUIRED since  wired the
    # reconcile-not-mirror drift gate into promote Step 3b (real gate, no stub
    # — the fixture source/target are framework-path subsets so it runs CLEAN,
    # and its weights-contract check self-skips: no goal-selector.py in the copy).
    for name in ("promote-to-upstream.sh", "_paths.sh", "_release_lib.py",
                 "check-releases-current.sh", "promotion-preflight.sh",
                 "promotion-preflight.py"):
        shutil.copy(CORE_SCRIPTS / name, src / "core" / "scripts" / name)
    (src / "core" / "scripts" / "seed-preflight.sh").write_text(_STUB_OK, encoding="utf-8")
    (src / "core" / "scripts" / "seed-verify.sh").write_text(_STUB_OK, encoding="utf-8")
    (src / "core" / "scripts" / "seed-transplant.sh").write_text(_STUB_TRANSPLANT, encoding="utf-8")
    (src / "core" / "config" / "compatibility.yaml").write_text(_FW_CHAIN_YAML, encoding="utf-8")
    (src / "mind_api" / "src" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    if frontier:
        (src / "RELEASES.json").write_text(json.dumps(
            [L.build_entry(version, None, "2026-06-05", False, False, "s", None, None, None)]),
            encoding="utf-8")
    (src / ".gitignore").write_text("core/scripts/.python-shim/\ncore/.pycache/\n", encoding="utf-8")
    _git(src, "init", "-q")
    _gitcfg(src)
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    _git(src, "branch", "-M", "main")
    if frontier:
        _git(src, "tag", "-a", f"v{version}", "-m", f"Release v{version}")  # Step 1c: HEAD == tag
    return src


def _mk_world(tmp_path: Path, role: str) -> Path:
    world = tmp_path / "world"
    (world / "config").mkdir(parents=True)
    (world / "config" / "compatibility.yaml").write_text(f"self_role: {role}\n", encoding="utf-8")
    return world


def _mk_target_with_remote(tmp_path: Path, version: str):
    """A target clone at `version` with a local bare repo wired as origin/main."""
    tgt = tmp_path / "tgt"
    (tgt / "mind_api" / "src").mkdir(parents=True)
    (tgt / "mind_api" / "src" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    _git(tgt, "init", "-q")
    _gitcfg(tgt)
    _git(tgt, "add", "-A")
    _git(tgt, "commit", "-q", "-m", "init")
    _git(tgt, "branch", "-M", "main")
    bare = tmp_path / "bare.git"
    r = subprocess.run([_GIT, "init", "--bare", "-q", str(bare)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _git(tgt, "remote", "add", "origin", bare.as_posix())
    _git(tgt, "push", "-q", "-u", "origin", "main")
    return tgt, bare


def _mk_gh_shim(tmp_path: Path):
    """A PATH-shim `gh` (bash script) that records its args to a capture file."""
    shim = tmp_path / "shim"
    shim.mkdir()
    cap = tmp_path / "gh-capture.txt"
    gh = shim / "gh"
    gh.write_text('#!/usr/bin/env bash\necho "$@" >> "$GH_CAPTURE_FILE"\nexit 0\n', encoding="utf-8")
    os.chmod(gh, 0o755)
    return shim, cap


def _run_promote_pr(src, target, world, *extra_args, shim_dir=None, gh_capture=None, extra_env=None):
    env = os.environ.copy()
    for k in ("MIND_AGENT", "MIND_SID", "WORLD_PATH", "META_PATH", "MIND_META", "MIND_GIT_AVAILABLE"):
        env.pop(k, None)
    env["MIND_WORLD"] = str(world)
    if shim_dir is not None:
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    if gh_capture is not None:
        env["GH_CAPTURE_FILE"] = str(gh_capture)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(src / "core" / "scripts" / "promote-to-upstream.sh"),
         "--target", str(target), "--pr", *extra_args],
        capture_output=True, text=True, env=env, cwd=str(src),
    )


@requires_git
def test_pr_creates_branch_before_plant(tmp_path):
    """F3 regression: the plant commit lands on the PR branch (pushed to the bare
    remote), NOT on main. Branch-created-AFTER-plant would put it on main."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, shim_dir=shim, gh_capture=cap)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "promote/v1.0.0" in _git(bare, "branch").stdout
    msg = _git(bare, "log", "promote/v1.0.0", "-1", "--pretty=%s").stdout
    assert "chore: sync framework" in msg


@requires_git
def test_pr_branch_content_correct(tmp_path):
    """The planted __init__.py on the PR branch carries the SOURCE version, and the
    plant commit is NOT on the bare remote's main (M2: agent never auto-merges)."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, shim_dir=shim, gh_capture=cap)
    assert r.returncode == 0, r.stdout + r.stderr
    show = _git(bare, "show", "promote/v1.0.0:mind_api/src/__init__.py").stdout
    assert '__version__ = "1.0.0"' in show
    main_log = _git(bare, "log", "main", "--oneline").stdout
    assert "sync framework" not in main_log


@requires_git
def test_pr_gh_args_captured(tmp_path):
    """The gh shim captures a correct `gh pr create` invocation (title + body)."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, shim_dir=shim, gh_capture=cap)
    assert r.returncode == 0, r.stdout + r.stderr
    captured = cap.read_text(encoding="utf-8")
    assert "pr create" in captured
    assert "--title" in captured and "Promote framework v1.0.0" in captured
    assert "--body" in captured and "Automated framework promotion" in captured


@requires_git
def test_pr_gh_missing_warns_not_fails(tmp_path):
    """gh absent: promote WARNS and exits 0 (does NOT push or open a PR — the plant
    stays committed on the local PR branch in the target).

    Forced deterministically via PROMOTE_GH_BIN="" (set-but-empty) instead of the old
    skipif(shutil.which("gh")): that was fragile because git-bash's `command -v` and
    Python's `shutil.which` search DIFFERENT PATHs on Windows, so the skip condition
    and the script disagreed about whether gh exists. The empty override forces the
    not-found branch regardless of what is installed."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    r = _run_promote_pr(src, tgt, world, extra_env={"PROMOTE_GH_BIN": ""})  # force gh-missing
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "not installed" in out
    # No push happened in the gh-missing branch: the bare remote has no PR branch.
    assert "promote/v1.0.0" not in _git(bare, "branch").stdout
    # But the plant IS committed on the local PR branch in the target.
    assert "promote/v1.0.0" in _git(tgt, "branch").stdout


@requires_git
def test_pr_gh_bin_override_used(tmp_path):
    """PROMOTE_GH_BIN points the wrapper at a gh binary even when none is on PATH —
    the Windows-git-bash case the fix addresses (gh installed off the MSYS PATH).
    The shim is NOT added to PATH (shim_dir omitted), so `command -v gh` fails;
    only the explicit override resolves it, and the resolved binary runs `pr create`."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, gh_capture=cap,
                        extra_env={"PROMOTE_GH_BIN": (shim / "gh").as_posix()})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "promote/v1.0.0" in _git(bare, "branch").stdout
    assert "pr create" in cap.read_text(encoding="utf-8")


@requires_git
def test_pr_push_failure_warns_not_fails(tmp_path):
    """A failed push (origin removed) is a WARNING, not a hard failure — promote
    still exits 0 and the plant remains on the local PR branch."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    _git(tgt, "remote", "remove", "origin")  # break the push
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, shim_dir=shim, gh_capture=cap)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING" in (r.stdout + r.stderr)
    assert "promote/v1.0.0" not in _git(bare, "branch").stdout


@requires_git
def test_pr_invariant_violation_rejects_before_push(tmp_path):
    """CW2: a target AHEAD of the source is refused at Step 2, BEFORE any branch
    is created or pushed (nothing reaches the bare remote)."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "99.0.0")
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, shim_dir=shim, gh_capture=cap)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "INVARIANT VIOLATION" in r.stderr
    assert "promote/v1.0.0" not in _git(bare, "branch").stdout
    assert not cap.exists()  # gh never invoked


@requires_git
def test_pr_custom_branch_name(tmp_path):
    """--branch overrides the default promote/vX.Y.Z branch name end-to-end."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, bare = _mk_target_with_remote(tmp_path, "0.0.1")
    shim, cap = _mk_gh_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, "--branch", "custom-promote-x",
                        shim_dir=shim, gh_capture=cap)
    assert r.returncode == 0, r.stdout + r.stderr
    # --branch took effect: the custom branch (not promote/v1.0.0) is what got pushed.
    pushed = _git(bare, "branch").stdout
    assert "custom-promote-x" in pushed
    assert "promote/v1.0.0" not in pushed
    # gh pr create infers the head from the checked-out branch (no branch arg), so the
    # branch name is NOT in the gh args — what we verify is that gh ran on this cut.
    assert "pr create" in cap.read_text(encoding="utf-8")


# ===========================================================================
# 7. promote-to-upstream.sh — dry-run happy path + publishability gate
#    (ISOLATED source, )
# ===========================================================================
# These exercise the dry-run "[dry-run] OK" path and the Step-3 seed-preflight
# gate against an ISOLATED clean+tagged source with stubbed sub-steps -- the same
# harness the --pr tests use (_setup_promote_source stubs seed-preflight -> exit 0).
# They were previously run against the LIVE repo via run_promote(), which coupled
# them to the live repo's incidental publishability: promote Step 3 invokes
# seed-preflight UNCONDITIONALLY (dry-run softens only the Step-1b/1c release-
# ceremony notes, NOT content publishability -- intentional since inception, commit
# c92777b1). Any committed publishability defect anywhere (e.g. a domain-token leak
# in a core/config design doc) failed both tests with returncode 1, in CI as well
# as during development; they were also the suite's two slowest tests (~140s each,
# running the real seed-preflight). Isolation makes them deterministic AND fast.
def _run_promote_dry(src, target, world, *extra_args, extra_env=None):
    """Run the ISOLATED source's promote in --dry-run against `target`."""
    env = os.environ.copy()
    for k in ("MIND_AGENT", "MIND_SID", "WORLD_PATH", "META_PATH", "MIND_META",
              "MIND_GIT_AVAILABLE"):
        env.pop(k, None)
    env["MIND_WORLD"] = str(world)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(src / "core" / "scripts" / "promote-to-upstream.sh"),
         "--target", str(target), "--dry-run", *extra_args],
        capture_output=True, text=True, env=env, cwd=str(src),
    )


@requires_git
def test_shell_dry_run_lower_target_ok(tmp_path):
    """Happy path: local >= target, all pre-flight + preflight pass, dry-run
    reaches OK and writes NOTHING (no seed-transplant, no PR). Isolated source so
    the result is independent of the live repo's publishability (g-115-1512)."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    below = L.bump_version("0.0.0", "patch")  # "0.0.1" -- <= source 1.0.0
    tgt = _mk_target(tmp_path, below)
    r = _run_promote_dry(src, tgt, world)
    assert r.returncode == 0, r.stderr
    assert "[dry-run] OK" in r.stdout
    assert "would: seed-transplant.sh" in r.stdout  # would, not did
    # : pin that the Step 3b reconcile-not-mirror drift gate RAN on the
    # happy path — removing the promotion-preflight invocation must fail here.
    assert "promotion-preflight: CLEAN" in r.stdout
    # The target was NOT mutated by the dry-run.
    assert (tgt / "mind_api" / "src" / "__init__.py").read_text(encoding="utf-8").strip() \
        == f'__version__ = "{below}"'


@requires_git
def test_shell_dry_run_equal_target_ok(tmp_path):
    """Invariant is >= (equal is allowed). Isolated source pinned to the real local
    __version__ so 'equal' is tested against a realistic version (g-115-1512)."""
    ver = _local_version()
    src = _setup_promote_source(tmp_path, ver)
    world = _mk_world(tmp_path, "frontier")
    tgt = _mk_target(tmp_path, ver)  # equal to the source version
    r = _run_promote_dry(src, tgt, world)
    assert r.returncode == 0, r.stderr
    assert "[dry-run] OK" in r.stdout


@requires_git
def test_shell_dry_run_unpublishable_fails(tmp_path):
    """Step 3 is a HARD gate even in --dry-run: when seed-preflight FAILs, dry-run
    refuses (returncode 1) and never reaches "[dry-run] OK". Pins the intentional
    design (dry-run validates content publishability) that the soft Step-1b/1c
    release-ceremony notes do NOT extend to -- the behavior whose implicitness
    drove the g-115-1512 investigation."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    # Override the OK stub: force seed-preflight to FAIL.
    (src / "core" / "scripts" / "seed-preflight.sh").write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    world = _mk_world(tmp_path, "frontier")
    tgt = _mk_target(tmp_path, "0.0.1")  # valid lower target -> passes Step 2
    r = _run_promote_dry(src, tgt, world)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "seed-preflight FAILED" in r.stderr
    assert "[dry-run] OK" not in r.stdout


# ===========================================================================
# 8. promote-to-upstream.sh — role-conditional release gates ()
#    seed->downstream promote runs WITHOUT --force-release; frontier stays strict
# ===========================================================================
@requires_git
def test_shell_dry_run_seed_role_skips_release_gates(tmp_path):
    """ (option 2): a SEED source (self_role=seed, NO RELEASES.json, NO
    v-tag — the un-bootstrapped claude-mind condition) promotes downstream in
    --dry-run WITHOUT --force-release. The RELEASES.json (Step 1a) and v-tag
    (Step 1c) gates are FRONTIER-ONLY provenance and skip for a non-frontier role.
    This is the seed->downstream half of the acceptance check '--dry-run passes
    from BOTH frontier and seed clones'."""
    src = _setup_promote_source(tmp_path, "1.0.0", frontier=False)  # no RELEASES.json, no tag
    world = _mk_world(tmp_path, "seed")
    tgt = _mk_target(tmp_path, "0.5.0")  # downstream below seed 1.0.0 -> invariant OK
    r = _run_promote_dry(src, tgt, world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[dry-run] OK" in r.stdout
    # Step 1a delegated to check-releases-current.sh, which PASSes a non-frontier
    # source with no RELEASES.json as N/A (version SSOT __version__ authoritative).
    assert "non-frontier" in r.stdout
    # Step 1c v-tag check skipped for the non-frontier role.
    assert "skip tag-check" in r.stdout


@requires_git
def test_shell_dry_run_frontier_missing_releases_still_fails(tmp_path):
    """SYMMETRY GUARD (no over-relaxation): the role-conditional skip is
    non-frontier ONLY. A FRONTIER source genuinely missing RELEASES.json MUST
    still FAIL — check-releases-current.sh FAILs a frontier with no release
    history. Proves the g-115-1811 relaxation did not weaken the frontier gate."""
    src = _setup_promote_source(tmp_path, "1.0.0", frontier=False)  # no RELEASES.json
    world = _mk_world(tmp_path, "frontier")  # ...but the overlay claims frontier
    tgt = _mk_target(tmp_path, "0.5.0")
    r = _run_promote_dry(src, tgt, world)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "RELEASES.json" in (r.stdout + r.stderr)
    assert "[dry-run] OK" not in r.stdout


# ---------------------------------------------------------------------------
# --auto-merge (). Opt-in merge of the PR the run just opened.
#
# The shim below is richer than _mk_gh_shim: it DISPATCHES on the gh subcommand
# so the script's real decision path executes. Scenarios are driven by env vars
# rather than by separate shims, so every test exercises one identical binary.
#
# Two branches here exist because a guardrail demanded them, and neither can be
# reproduced on demand against live GitHub:
#   guard-1897 — `gh pr merge` prints `fatal: Not possible to fast-forward` and
#                exits NON-ZERO when the REMOTE MERGE ALREADY SUCCEEDED (the
#                fatal comes from gh updating the local checkout afterwards).
#                The verdict must come from the remote read, never the exit code.
#   guard-2640 — an EMPTY statusCheckRollup is not CI-green; it is an empty
#                population reporting clean, and it renders identically to a
#                real green. Measured 2026-08-10: no chain repo has any
#                .github/workflows, so [] is the PERMANENT state here.
# ---------------------------------------------------------------------------

_GH_DISPATCH_SHIM = r'''#!/usr/bin/env bash
echo "$@" >> "$GH_CAPTURE_FILE"
if [[ "$1 $2" == "pr create" ]]; then
  [[ -n "${SHIM_CREATE_PREAMBLE:-}" ]] && echo "$SHIM_CREATE_PREAMBLE"
  echo "https://github.com/acme/widget/pull/42"
elif [[ "$1 $2" == "pr view" ]]; then
  if [[ "$*" == *mergedAt* ]]; then printf '%b\n' "${SHIM_MERGED_AT-2026-08-10T07:00:00Z}"
  else
    _n=0
    [[ -f "$GH_CAPTURE_FILE.n" ]] && _n=$(cat "$GH_CAPTURE_FILE.n")
    _n=$((_n+1)); echo "$_n" > "$GH_CAPTURE_FILE.n"
    if [[ "$_n" == "1" && -n "${SHIM_PR_FACTS_FIRST:-}" ]]; then printf '%b\n' "$SHIM_PR_FACTS_FIRST"
    else printf '%b\n' "${SHIM_PR_FACTS-MERGEABLE\t0\t0}"; fi
  fi
elif [[ "$1 $2" == "pr merge" ]]; then
  if [[ -n "${SHIM_MERGE_FATAL:-}" ]]; then
    echo "fatal: Not possible to fast-forward, aborting." >&2
    exit 1
  fi
fi
exit 0
'''


def _mk_gh_dispatch_shim(tmp_path: Path):
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    cap = tmp_path / "gh-capture.txt"
    gh = shim / "gh"
    gh.write_text(_GH_DISPATCH_SHIM, encoding="utf-8")
    os.chmod(gh, 0o755)
    return shim, cap


def _auto_merge_run(tmp_path, *extra_args, **shim_env):
    """One full promotion with the dispatch shim. Returns (result, capture-text)."""
    src = _setup_promote_source(tmp_path, "1.0.0")
    world = _mk_world(tmp_path, "frontier")
    tgt, _bare = _mk_target_with_remote(tmp_path, "0.0.1")
    shim, cap = _mk_gh_dispatch_shim(tmp_path)
    r = _run_promote_pr(src, tgt, world, *extra_args,
                        shim_dir=shim, gh_capture=cap, extra_env=shim_env)
    return r, (cap.read_text(encoding="utf-8") if cap.exists() else "")


@requires_git
def test_auto_merge_off_by_default(tmp_path):
    """Backward compatibility: without the flag the script still NEVER merges."""
    r, cap = _auto_merge_run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr create" in cap
    assert "pr merge" not in cap


@requires_git
def test_auto_merge_merges_when_mergeable_with_zero_checks(tmp_path):
    """guard-2640: zero checks is NOT laundered into 'CI passed' — the script
    says so explicitly and names the local gate it is actually relying on."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge", SHIM_PR_FACTS="MERGEABLE\\t0\\t0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" in cap
    assert "NOT read as CI-green" in r.stdout
    assert "seed-verify.sh --expect-commit passed" in r.stdout
    assert "--auto-merge OK" in r.stdout


@requires_git
def test_auto_merge_refuses_when_not_mergeable(tmp_path):
    """A conflicted PR is never merged, and the PR is left open."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge", SHIM_PR_FACTS="CONFLICTING\\t0\\t0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" not in cap
    assert "REFUSED" in r.stdout and "CONFLICTING" in r.stdout
    assert "left OPEN" in r.stdout


@requires_git
def test_auto_merge_refuses_when_checks_failing(tmp_path):
    """Checks that EXIST and are red block the merge (guard-1199/1264)."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge", SHIM_PR_FACTS="MERGEABLE\\t3\\t1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" not in cap
    assert "REFUSED" in r.stdout
    assert "1 of 3 status check(s) not SUCCESS" in r.stdout


@requires_git
def test_auto_merge_proceeds_when_checks_all_green(tmp_path):
    """The positive control for the branch above: same shape, 0 bad -> merges."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge", SHIM_PR_FACTS="MERGEABLE\\t3\\t0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" in cap
    assert "3 status check(s) all SUCCESS" in r.stdout
    assert "NOT read as CI-green" not in r.stdout


@requires_git
def test_auto_merge_trusts_remote_over_gh_exit_status(tmp_path):
    """guard-1897: gh pr merge exits NON-ZERO with the fast-forward fatal while
    the remote merge SUCCEEDED. Reading the exit code would report a false
    failure; the remote mergedAt read is the only verdict."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge",
                             SHIM_PR_FACTS="MERGEABLE\\t0\\t0",
                             SHIM_MERGE_FATAL="1",
                             SHIM_MERGED_AT="2026-08-10T07:11:00Z")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" in cap
    assert "--auto-merge OK" in r.stdout
    assert "2026-08-10T07:11:00Z" in r.stdout
    assert "did NOT land" not in r.stdout


@requires_git
def test_auto_merge_reports_when_remote_says_not_merged(tmp_path):
    """The other half of guard-1897: a clean gh exit is not success either.
    Empty mergedAt at the remote must surface as a WARNING, not as OK."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge",
                             SHIM_PR_FACTS="MERGEABLE\\t0\\t0",
                             SHIM_MERGED_AT="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" in cap
    assert "did NOT land" in r.stdout
    assert "--auto-merge OK" not in r.stdout


def test_auto_merge_requires_pr_flag(tmp_path):
    """An accepted-but-inert flag is worse than a rejected one (guard-386)."""
    r = subprocess.run(
        [BASH, str(PROMOTE_SH), "--target", str(tmp_path), "--auto-merge"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--auto-merge requires --pr" in (r.stdout + r.stderr)


@requires_git
def test_auto_merge_polls_through_unknown_mergeable(tmp_path):
    """GitHub computes `mergeable` asynchronously: a just-created PR commonly
    reads UNKNOWN and resolves on a later read (measured 2026-08-10 against a
    live PR — poll 1 UNKNOWN, poll 2 MERGEABLE). A single-shot read would refuse
    almost every real promotion while passing every mocked test, so the poll is
    load-bearing and this pins it."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge",
                             SHIM_PR_FACTS_FIRST="UNKNOWN\\t0\\t0",
                             SHIM_PR_FACTS="MERGEABLE\\t0\\t0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "mergeable=UNKNOWN" in r.stdout and "re-reading" in r.stdout
    assert "pr merge" in cap
    assert "--auto-merge OK" in r.stdout


@requires_git
def test_auto_merge_refuses_when_mergeable_never_resolves(tmp_path):
    """The other side of the poll: UNKNOWN that never resolves is a REFUSAL, not
    consent. Retrying must not decay into merging on an unresolved signal."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge", SHIM_PR_FACTS="UNKNOWN\\t0\\t0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr merge" not in cap
    assert "REFUSED" in r.stdout and "UNKNOWN" in r.stdout


@requires_git
def test_auto_merge_survives_extra_stdout_from_pr_create(tmp_path):
    """F-001 (fresh-eyes on this goal's own diff): gh may print an advisory line
    on stdout BEFORE the PR URL. Capturing the whole of stdout makes PR_URL
    multi-line, which `gh pr view` then rejects — turning a healthy PR into a
    false UNREADABLE -> REFUSED. Only the last non-empty line is the URL."""
    r, cap = _auto_merge_run(tmp_path, "--auto-merge",
                             SHIM_CREATE_PREAMBLE="Warning: 3 uncommitted changes",
                             SHIM_PR_FACTS="MERGEABLE\\t0\\t0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PR opened: https://github.com/acme/widget/pull/42" in r.stdout
    assert "Warning: 3 uncommitted changes" not in r.stdout.split("PR opened:")[1][:80]
    assert "pr merge" in cap
    assert "--auto-merge OK" in r.stdout
    assert "REFUSED" not in r.stdout
