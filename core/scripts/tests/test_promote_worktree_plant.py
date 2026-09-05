#!/usr/bin/env python3
""" — the promotion plant must never switch the target's live branch.

WHAT IS UNDER TEST, and why it is extracted rather than copied
--------------------------------------------------------------
`promote-to-upstream.sh` Step 4 used to run

    ( cd "$TARGET" && git checkout -b "$BRANCH" ... )

which repoints the TARGET DEPLOYMENT'S LIVE CHECKOUT at an unmerged branch and
leaves it there — on success, on a failed plant, on a failed verify, and on a
crash. The replacement adds an isolated worktree and plants into that instead.

These tests EXTRACT the Step 4 block from the shipped script and execute those
exact bytes. They do not re-type the logic. A hand-written copy would be a
second implementation that passes while the real one rots (guard-920: test the
production shape, not the contract-ideal one) — and the extraction is asserted
non-empty and self-consistent, so a rename of the section markers fails loudly
here instead of silently reducing this file to testing nothing.

WHAT THIS FILE DOES NOT COVER (guard-1462 — name the excluded layers)
--------------------------------------------------------------------
The seam is Step 4 alone. Everything downstream of it — the seed-transplant
plan, the plant, seed-verify, the `gh pr create` call — is NOT exercised here;
those need a full promotion run. What IS covered is the mechanism the goal
names: branch creation, plant-directory isolation, commit reachability, push
from the worktree, and teardown. Step 4 was additionally exercised by hand
against the real downstream clone during g-115-4803 and the clone restored
byte-for-byte; that run is evidence, not coverage, and is not repeated here.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402

REPO = Path(__file__).resolve().parents[2].parent
SCRIPT = REPO / "core" / "scripts" / "promote-to-upstream.sh"

# The section boundaries in the shipped script. Both must match exactly once.
START = "# --- Step 4: (if --pr) create the PR branch in an ISOLATED WORKTREE"
END = "# --- Step 4a: living-prod blast-radius gate"

# The pre- shape, kept verbatim as the NEGATIVE CONTROL. A test that
# only ever runs the new code cannot distinguish "the fix works" from "the
# assertion is weak" — this block is what gives the suite discriminating power.
LEGACY_STEP4 = """
PLANT_DIR="$TARGET"
if [[ $DO_PR -eq 1 ]]; then
  ( cd "$TARGET" && { git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"; } ) \\
    || fail "could not create/switch to PR branch '$BRANCH' in $TARGET"
fi
"""

PRELUDE = """
set -euo pipefail
SCRIPT_DIR="{script_dir}"
TARGET="{target}"
BRANCH="{branch}"
DO_PR={do_pr}
say() {{ echo "[promote] $*"; }}
fail() {{ echo "[promote] ERROR: $*" >&2; exit 1; }}
"""

# Printed after the block so the harness can read back what the block decided.
#
# `trap - EXIT` disarms the teardown, and that is a deliberate fidelity choice,
# not a convenience. In production the EXIT trap fires at the end of the WHOLE
# promotion — after the plan, the plant, the verify and the push. Here the
# extracted block IS the whole script, so leaving the trap armed would tear the
# worktree down microseconds after creating it and every assertion below would
# be inspecting the state of a run that had already finished. Disarming makes
# the harness observe Step 4's output at the moment the rest of the promotion
# would see it. The trap itself is covered separately, statically, by
# test_teardown_is_invoked_on_every_exit_path.
EPILOGUE = """
trap - EXIT
echo "PLANT_DIR=$PLANT_DIR"
echo "LIVE_BRANCH=$(git -C "$TARGET" rev-parse --abbrev-ref HEAD)"
"""


def extract_step4() -> str:
    """Return the Step 4 block from the shipped script, or fail loudly."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(START) == 1, f"START marker not unique in {SCRIPT}"
    assert text.count(END) == 1, f"END marker not unique in {SCRIPT}"
    block = text[text.index(START):text.index(END)]
    assert len(block.splitlines()) > 20, "extracted Step 4 block is implausibly short"
    return block


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def target(tmp_path):
    """A scratch repo standing in for a downstream deployment clone."""
    t = tmp_path / "target"
    t.mkdir()
    git("init", "-q", "-b", "main", cwd=t)
    git("config", "user.email", "t@example.invalid", cwd=t)
    git("config", "user.name", "t", cwd=t)
    (t / "CLAUDE.md").write_text("deployment-local\n", encoding="utf-8")
    git("add", "-A", cwd=t)
    git("commit", "-qm", "base", cwd=t)
    return t


def run_block(block, target, branch="promote/v9.9.9", do_pr=1, env=None):
    body = PRELUDE.format(
        script_dir=(REPO / "core" / "scripts").as_posix(),
        target=target.as_posix(),
        branch=branch,
        do_pr=do_pr,
    ) + block + EPILOGUE
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(body)
        path = fh.name
    try:
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run(
            [BASH, Path(path).as_posix()], capture_output=True, text=True, env=e
        )
    finally:
        os.unlink(path)


def parsed(proc):
    out = {}
    for line in proc.stdout.splitlines():
        if "=" in line and line.split("=", 1)[0] in ("PLANT_DIR", "LIVE_BRANCH"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


# --------------------------------------------------------------------------
# The claim: the live checkout's branch is untouched.
# --------------------------------------------------------------------------

def test_live_checkout_branch_never_switches(target):
    before = git("rev-parse", "--abbrev-ref", "HEAD", cwd=target)
    proc = run_block(extract_step4(), target)
    assert proc.returncode == 0, proc.stderr
    after = git("rev-parse", "--abbrev-ref", "HEAD", cwd=target)
    assert before == after == "main", (
        f"live checkout moved {before!r} -> {after!r}; stderr={proc.stderr}"
    )
    assert parsed(proc)["LIVE_BRANCH"] == "main"


def test_negative_control_legacy_shape_DOES_switch_the_live_branch(target):
    """Proves the assertion above can fail — i.e. it has discriminating power.

    Without this, `test_live_checkout_branch_never_switches` would also pass
    against a Step 4 that did nothing at all.
    """
    proc = run_block(LEGACY_STEP4, target)
    assert proc.returncode == 0, proc.stderr
    after = git("rev-parse", "--abbrev-ref", "HEAD", cwd=target)
    assert after == "promote/v9.9.9", (
        "the legacy shape was expected to switch the live checkout; if this "
        "fails the harness is not exercising what it claims to"
    )


def test_plant_dir_is_a_separate_worktree_on_the_pr_branch(target):
    proc = run_block(extract_step4(), target)
    assert proc.returncode == 0, proc.stderr
    plant = Path(parsed(proc)["PLANT_DIR"])
    assert plant != target and plant.is_dir(), f"PLANT_DIR not isolated: {plant}"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=plant) == "promote/v9.9.9"
    # The tracked deployment-local file is present in the worktree — this is
    # what lets --living-prod preserve it during the plant.
    assert (plant / "CLAUDE.md").is_file()


def test_plant_dir_is_target_itself_when_pr_is_not_requested(target):
    """The no --pr flow must keep its existing behaviour exactly."""
    proc = run_block(extract_step4(), target, do_pr=0)
    assert proc.returncode == 0, proc.stderr
    assert parsed(proc)["PLANT_DIR"] == str(target)
    # --path-format=absolute is load-bearing, not decoration. `rev-parse
    # --git-common-dir` answers RELATIVE to the git invocation's cwd (".git"),
    # and Path(".git")/"worktrees" then resolves against the PYTEST process's
    # cwd -- the live repo -- not against `target`. So this assertion used to
    # read /opt/<repo>/.git/worktrees and went red whenever the developer had
    # any worktree checked out, indistinguishably from a real regression
    # (guard-5842, which described the collision as a procedure problem; the
    # defect is here). Same idiom as worktree-teardown.sh:120.
    common = Path(git("rev-parse", "--path-format=absolute",
                      "--git-common-dir", cwd=target))
    assert common.is_absolute(), f"git-common-dir not absolute: {common}"
    assert not (common / "worktrees").exists()


def test_git_common_dir_assertion_is_scoped_to_the_fixture_not_the_live_repo(target):
    """Regression pin () for the assertion directly above.

    `rev-parse --git-common-dir` answers RELATIVE to the git invocation's cwd,
    so `Path(".git") / "worktrees"` resolves against the PYTEST PROCESS cwd --
    the live repo -- not against `target`. The sibling test therefore went red
    for anyone holding a worktree in the live checkout, indistinguishably from
    a real regression. guard-5842 recorded that collision as a problem with the
    developer's procedure; it is this line. Pin both halves so the naive form
    cannot silently return.
    """
    assert Path.cwd() != target, "premise gone: pytest is running inside the fixture"
    naive = Path(git("rev-parse", "--git-common-dir", cwd=target))
    absolute = Path(git("rev-parse", "--path-format=absolute",
                        "--git-common-dir", cwd=target))
    assert not naive.is_absolute(), (
        "git now answers absolutely by default -- re-derive this pin, do not delete it")
    assert absolute.is_absolute()
    assert absolute.parent == target, absolute
    # The naive form escapes the fixture; the absolute form cannot.
    assert naive.resolve() != absolute.resolve()


def test_commit_in_worktree_is_reachable_from_target_after_teardown(target):
    """Teardown loses the directory, never the work."""
    proc = run_block(extract_step4(), target)
    plant = Path(parsed(proc)["PLANT_DIR"])
    (plant / "planted.txt").write_text("payload\n", encoding="utf-8")
    git("add", "-A", cwd=plant)
    git("commit", "-qm", "plant", cwd=plant)
    sha = git("rev-parse", "HEAD", cwd=plant)

    td = subprocess.run(
        [BASH, (REPO / "core" / "scripts" / "worktree-teardown.sh").as_posix(),
         str(plant), "--force", "--quiet"],
        capture_output=True, text=True,
    )
    assert td.returncode == 0, f"teardown failed: {td.stdout}{td.stderr}"
    assert not plant.exists(), "worktree directory survived teardown"
    # The branch and its commit live in the target's object store.
    assert git("rev-parse", "promote/v9.9.9", cwd=target) == sha
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=target) == "main"


def test_rerun_reuses_an_existing_branch_instead_of_failing(target):
    """A re-run after a failed promotion must not die on 'branch exists'."""
    first = run_block(extract_step4(), target)
    plant = Path(parsed(first)["PLANT_DIR"])
    subprocess.run(
        [BASH, (REPO / "core" / "scripts" / "worktree-teardown.sh").as_posix(),
         str(plant), "--force", "--quiet"], capture_output=True, text=True,
    )
    second = run_block(extract_step4(), target)
    assert second.returncode == 0, (
        f"re-run against an existing branch failed: {second.stderr}"
    )
    assert Path(parsed(second)["PLANT_DIR"]) != plant


def test_push_from_the_worktree_reaches_the_targets_own_remote(tmp_path, target):
    """The mechanism half of `PR branch pushed from worktree`.

    A worktree shares its parent's git dir, so `origin` and the refs are the
    same objects the live checkout sees — but that is the kind of claim worth
    measuring rather than reasoning about, because if it were false the push
    would land somewhere else or nowhere. A LOCAL bare remote is used
    deliberately: it exercises the whole push path (remote resolution, ref
    update, upstream tracking) without an outward-facing write.

    NOT covered here: an end-to-end promotion against a real downstream clone,
    and `gh pr create`. Those need a live promotion run, which belongs to the
    goal that owns it, not to this one.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git("remote", "add", "origin", str(bare), cwd=target)

    proc = run_block(extract_step4(), target)
    assert proc.returncode == 0, proc.stderr
    plant = Path(parsed(proc)["PLANT_DIR"])

    (plant / "planted.txt").write_text("payload\n", encoding="utf-8")
    git("add", "-A", cwd=plant)
    git("commit", "-qm", "plant", cwd=plant)
    sha = git("rev-parse", "HEAD", cwd=plant)

    # This is the shape promote-to-upstream.sh runs: cd into the worktree.
    pushed = subprocess.run(
        ["git", "push", "-u", "origin", "promote/v9.9.9"],
        cwd=str(plant), capture_output=True, text=True,
    )
    assert pushed.returncode == 0, f"{pushed.stdout}{pushed.stderr}"

    # The ref landed on the remote, carrying the worktree's commit.
    assert git("rev-parse", "promote/v9.9.9", cwd=bare) == sha
    # Upstream tracking was set on the shared ref, so the live checkout agrees.
    assert git("rev-parse", "refs/remotes/origin/promote/v9.9.9", cwd=target) == sha
    # And the live checkout still never moved.
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=target) == "main"


def test_target_already_on_the_pr_branch_fails_with_a_remediation(target):
    """The residue of a PRE-fix run: the live checkout is ON the promote branch.

    git's own error ('already used by worktree at ...') names the symptom and
    not the fix, and this is the state the code being replaced leaves behind —
    so the first run of the new version against a previously-promoted target
    hits it. The refusal must be diagnostic, and must mutate nothing.
    """
    git("checkout", "-q", "-b", "promote/v9.9.9", cwd=target)
    proc = run_block(extract_step4(), target)

    assert proc.returncode != 0, "expected a refusal, not a silent success"
    combined = proc.stdout + proc.stderr
    assert "checkout main" in combined, (
        f"refusal does not name the remediation:\n{combined}"
    )
    assert "Nothing has been mutated" in combined
    # And it really did not mutate: no worktree was registered.
    assert "worktree" not in git("worktree", "list", cwd=target).replace(str(target), "")


# --------------------------------------------------------------------------
# Wiring: the downstream steps must read PLANT_DIR, not TARGET.
# A worktree that nothing plants into would pass every test above.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "marker",
    [
        'seed-transplant.sh" "$PLANT_DIR" --living-prod --plan',
        'seed-transplant.sh" "$PLANT_DIR" $LP_FLAG --force --commit',
        'seed-verify.sh" "$PLANT_DIR" --expect-commit',
        'cd "$PLANT_DIR" && git push -u origin "$BRANCH"',
    ],
)
def test_downstream_steps_operate_on_plant_dir(marker):
    assert marker in SCRIPT.read_text(encoding="utf-8"), (
        f"{marker!r} missing — a downstream step still targets the live "
        f"checkout, so the worktree isolation is cosmetic"
    )


def test_living_prod_autodetect_still_reads_the_real_clone():
    """The ONE deliberate exception: detection must stay on $TARGET.

    `.mind-data` is gitignored and therefore absent from any worktree, so
    detecting against PLANT_DIR would read a living production deployment as a
    fresh seed and drop --living-prod — clobbering the deployment-local files
    that flag exists to preserve.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    block = text[text.index("# --- Step 3c: living-prod detection"):
                 text.index("# --- Dry-run stops here")]
    assert '-d "$TARGET/.mind-data"' in block
    assert "PLANT_DIR" not in block, (
        "living-prod detection must not consult PLANT_DIR — see Step 4a note"
    )


# --------------------------------------------------------------------------
# The goal's OPEN QUESTION, in its substantive form: a worktree lacks every
# GITIGNORED file, and the seed engine reads `agents/*/local-paths.conf` to
# find the destination's forged-skill registry. Does planting into a worktree
# therefore delete the deployment's resident forged skills?
#
# It does not — the resolver returns None (not an empty set) when it cannot
# locate a registry, and all four call sites read None as "protect every skill
# dir". These tests pin that, because the failure direction of a "cleanup" that
# swapped None for set() is silent deletion of downstream domain capability.
# --------------------------------------------------------------------------

def _seed_engine():
    import sys
    sys.path.insert(0, str(REPO / "core" / "scripts"))
    import _seed_engine
    return _seed_engine


def test_worktree_shaped_destination_protects_all_skill_dirs(tmp_path):
    E = _seed_engine()
    wt = tmp_path / "worktree"
    (wt / "agents" / "alpha").mkdir(parents=True)   # dir tracked, conf gitignored
    skill = wt / ".claude" / "skills" / "resident-domain-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    assert E._dest_forged_skill_names(wt) is None, (
        "an unlocatable registry must return None, not an empty set — None is "
        "what every call site reads as protect_all_skills"
    )
    # Second, independent signal: SKILL.md presence is a TRACKED artefact, so
    # it survives into the worktree even though the registry does not.
    assert E._dest_skill_names_with_skillmd(wt) == {"resident-domain-skill"}
    # A worktree has no in-repo world/meta store, so nothing needs protecting.
    assert E._in_repo_store_tops(wt) == set()


def test_registry_resolution_positive_control(tmp_path):
    """Proves the None above is the absent-registry branch, not a constant."""
    E = _seed_engine()
    dest = tmp_path / "withreg"
    ext = tmp_path / "extworld"
    ext.mkdir()
    (ext / "forged-skills.yaml").write_text(
        "skills:\n  conf-routed-skill: {}\n", encoding="utf-8")
    conf = dest / "agents" / "alpha"
    conf.mkdir(parents=True)
    (conf / "local-paths.conf").write_text(f"WORLD_PATH={ext}\n", encoding="utf-8")

    assert E._dest_forged_skill_names(dest) == {"conf-routed-skill"}
    # Remove ONLY the gitignored conf — the single variable a worktree changes.
    (conf / "local-paths.conf").unlink()
    assert E._dest_forged_skill_names(dest) is None


def test_teardown_is_invoked_on_every_exit_path():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "trap _wt_teardown EXIT" in text, "no teardown on the failure paths"
    assert re.search(r"_wt_torn_down=1", text), "teardown is not idempotent"
