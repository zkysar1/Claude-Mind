"""The dirty-working-tree check must FAIL CLOSED on any git error ().

Five sites in the promotion chain captured `git status --porcelain` with stderr
sent to /dev/null and the exit code dropped, so ANY git failure produced an
EMPTY string that every caller read as "the working tree is clean".

WHY THAT IS DANGEROUS HERE RATHER THAN MERELY UNTIDY: the likeliest failure on
this fleet is .git/index.lock contention from a partner's concurrent
iteration-commit.sh, so the check was least trustworthy exactly when partners
are committing — the live-fleet condition the g-115-3514 TOCTOU re-check exists
for. That re-check calls the same predicate a second time, so a wedged git made
BOTH calls report clean. release.sh is the worst site: it is the sole v-tagger,
so a fail-open there mints a version tag on a dirty tree.

WHY THIS FILE EXISTS AT ALL. The 12 tests in test_promote_source_provenance_toctou
all run against a HEALTHY temp repo, so the fail-open branch had ZERO coverage
while the suite was green — guard-1943's shape moved from the production caller
into the test harness. Every test here induces a REAL git failure.

Three induction modes, deliberately, because they fail in different ways:
  A. non-repo directory        -> git exits 128 WITH stderr
  B. PATH-stubbed failing git  -> arbitrary rc WITH stderr (stands in for
                                  index.lock contention, which is awkward to
                                  provoke deterministically)
  C. PATH-stubbed SILENT fail  -> non-zero rc and NO stderr at all

Mode C is the one that separates the two candidate fixes. Merely routing stderr
into the capture (2>&1) is not enough: a silent non-zero exit still yields an
empty string and still reads as clean. Only checking rc explicitly catches it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from _bash_helpers import BASH  # noqa: E402

PROMOTE = SCRIPTS / "promote-to-upstream.sh"
RELEASE = SCRIPTS / "release.sh"
TRANSPLANT = SCRIPTS / "seed-transplant.sh"
VERIFY = SCRIPTS / "seed-verify.sh"


# ------------------------------------------------------------- git stubs --

def _stub_git(tmp_path: Path, body: str) -> dict:
    """A PATH whose `git` runs `body`. Returns an env dict for subprocess.

    Stands in for index.lock contention: what matters downstream is a non-zero
    exit, not which git operation produced it.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    git = bindir / "git"
    git.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    git.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    return env


FAIL_LOUD = 'echo "fatal: unable to create \'.git/index.lock\': File exists" >&2\nexit 128'
FAIL_SILENT = "exit 128"   # non-zero, NOTHING on stderr — mode C


def _bash(script: str, env=None, cwd=None):
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          timeout=60, env=env, cwd=cwd)


def _real_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"],
                 ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo)] + args, check=True,
                       capture_output=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "i"],
                   check=True, capture_output=True)
    return repo


# ------------------------------------------------- extract-under-test bits --

def _extract(path: Path, start_pat: str, end_pat: str) -> str:
    """Lift a block out of a script so it can run without the whole script.

    Anchored on the literal source text: if the block is renamed or reshaped,
    extraction fails loudly here rather than silently testing nothing.
    """
    src = path.read_text(encoding="utf-8").splitlines()
    s = next((i for i, l in enumerate(src) if re.search(start_pat, l)), None)
    assert s is not None, f"start anchor {start_pat!r} not found in {path.name}"
    e = next((i for i, l in enumerate(src[s:], s) if re.search(end_pat, l)), None)
    assert e is not None, f"end anchor {end_pat!r} not found in {path.name}"
    return "\n".join(src[s:e + 1])


def _promote_predicate() -> str:
    return _extract(PROMOTE, r"^source_provenance_drift\(\) \{", r"^\}")


def _transplant_helper() -> str:
    return _extract(TRANSPLANT, r"^git_status_or_dirty\(\) \{", r"^\}")


def _release_block() -> str:
    """Capture PLUS the refusal branch.

    Not `_extract(..., r'^fi$')`: there are TWO column-0 `fi` lines in this
    region and the first closes the rc-check, so a naive end-anchor lifts the
    capture WITHOUT the refusal and the harness then reports rc=0 for every
    input — a test that cannot fail. Anchor on the refusal `if` and take the
    `fi` after it.
    """
    src = RELEASE.read_text(encoding="utf-8").splitlines()
    s = next(i for i, l in enumerate(src) if re.match(r"^DIRTY_RC=0", l))
    guard = next(i for i, l in enumerate(src[s:], s)
                 if re.search(r'^if \[\[ -n "\$DIRTY" \]\]', l))
    e = next(i for i, l in enumerate(src[guard:], guard) if re.match(r"^fi\s*$", l))
    block = "\n".join(src[s:e + 1])
    assert "fail " in block, "extraction lost the refusal branch"
    return block


# =========================================================== the predicate ==

def _run_promote(repo_path: str, env=None):
    script = "\n".join([
        "set -uo pipefail",
        f'PROJECT_ROOT="{repo_path}"',
        'SELF_ROLE="frontier"', 'LOCAL="9.9.9"',
        _promote_predicate(),
        "if source_provenance_drift; then RC=0; else RC=$?; fi",
        'printf "%s|%s|%s" "$RC" "${SRC_DRIFT_KIND:-}" "${SRC_DRIFT_DETAIL:-}"',
    ])
    p = _bash(script, env=env)
    assert p.returncode == 0, f"harness itself failed: {p.stderr}"
    rc, kind, detail = p.stdout.split("|", 2)
    return int(rc), kind, detail


def test_promote_non_repo_reports_drift(tmp_path):
    """Mode A. Pre-fix this returned rc=0/no-drift for a directory that is not
    even a git repository."""
    rc, kind, detail = _run_promote(str(tmp_path))
    assert rc != 0, "a git failure must report DRIFT, not a clean tree"
    assert "dirty" in kind, kind


@pytest.mark.parametrize("body,label", [(FAIL_LOUD, "loud"), (FAIL_SILENT, "silent")])
def test_promote_failing_git_reports_drift(tmp_path, body, label):
    """Modes B and C. The SILENT case is the load-bearing one: routing stderr
    into the capture does nothing for it, so it passes only because rc is
    checked explicitly."""
    repo = _real_repo(tmp_path)
    env = _stub_git(tmp_path, body)
    rc, kind, detail = _run_promote(str(repo), env=env)
    assert rc != 0, f"{label} git failure must report DRIFT"
    assert "dirty" in kind
    assert "git status failed" in detail, detail


def test_promote_clean_repo_is_still_clean(tmp_path):
    """The fix must not make everything dirty — a fail-closed check that never
    passes is just a broken check (guard-2982's vacuity, inverted)."""
    repo = _real_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "tag", "v9.9.9"], check=True,
                   capture_output=True)
    rc, kind, _ = _run_promote(str(repo))
    assert rc == 0 and kind == "", f"clean repo at tag must not be drift: {kind}"


# ================================================== MUTATION / POSITIVE CTL ==

@pytest.mark.parametrize("body", [FAIL_LOUD, FAIL_SILENT])
def test_prefix_form_reports_clean_on_the_same_failure(tmp_path, body):
    """POSITIVE CONTROL — the fixture must be RED against the pre-fix code.

    Re-implements the old capture verbatim and asserts it reports CLEAN under
    exactly the failures the tests above catch. Without this, every assertion
    here would also pass against a parser that simply never fails, and the
    suite could not distinguish the fix from luck.
    """
    repo = _real_repo(tmp_path)
    env = _stub_git(tmp_path, body)
    old = ('_d="$(git -C "$R" status --porcelain 2>/dev/null || true)"; '
           'if [ -n "$_d" ]; then echo DIRTY; else echo CLEAN; fi')
    p = _bash(f'R="{repo}"\n{old}', env=env)
    assert p.stdout.strip() == "CLEAN", (
        "the pre-fix form no longer fails open — this fixture can no longer "
        "demonstrate the defect, so it cannot prove the fix")


@pytest.mark.parametrize("body", [FAIL_LOUD, FAIL_SILENT])
def test_fixed_form_reports_dirty_on_the_same_failure(tmp_path, body):
    """The other half of the control: same induced failure, new form, DIRTY."""
    repo = _real_repo(tmp_path)
    env = _stub_git(tmp_path, body)
    new = "\n".join([
        _transplant_helper(),
        f'if [ -n "$(git_status_or_dirty "{repo}")" ]; then echo DIRTY; else echo CLEAN; fi',
    ])
    p = _bash(new, env=env)
    assert p.stdout.strip() == "DIRTY", p.stdout + p.stderr


# ======================================================= seed-transplant ====

def test_transplant_helper_is_dirty_on_failure_and_clean_on_clean(tmp_path):
    repo = _real_repo(tmp_path)
    helper = _transplant_helper()
    clean = _bash(f'{helper}\nprintf "[%s]" "$(git_status_or_dirty "{repo}")"')
    assert clean.stdout == "[]", f"clean repo must yield empty: {clean.stdout!r}"
    env = _stub_git(tmp_path, FAIL_SILENT)
    failed = _bash(f'{helper}\nprintf "%s" "$(git_status_or_dirty "{repo}")"', env=env)
    assert "treating as DIRTY" in failed.stdout, failed.stdout


def test_both_transplant_call_sites_route_through_the_helper():
    """Two sites, one predicate. A site that drifts back to a raw capture would
    fail open again while this file stayed green."""
    src = TRANSPLANT.read_text(encoding="utf-8")
    # The definition reads `git_status_or_dirty() {`, so it does NOT match the
    # call shape — count calls only, and assert the definition separately.
    assert "git_status_or_dirty() {" in src, "helper definition is missing"
    assert src.count('git_status_or_dirty "') == 2, (
        f"expected exactly the two call sites (3b destination, 3e source), "
        f"found {src.count('git_status_or_dirty ')}")
    # Exactly ONE live status capture may exist — the helper's own. The point
    # is that no CALL SITE captures directly; a blanket "no status --porcelain"
    # assertion would flag the sanctioned capture itself.
    live = [l for l in src.splitlines()
            if "status --porcelain" in l and not l.lstrip().startswith("#")]
    assert len(live) == 1, f"expected only the helper's capture, got: {live}"
    assert "2>&1" in live[0] and "2>/dev/null" not in live[0], live[0]
    assert "no-optional-locks" in live[0], live[0]


# ============================================================== release.sh ==

def _run_release_block(repo: str, dry: int, env=None):
    script = "\n".join([
        "set -uo pipefail",
        f'PROJECT_ROOT="{repo}"', f"DRY={dry}",
        'say() { echo "SAY: $*"; }',
        'fail() { echo "FAIL: $*" >&2; exit 1; }',
        _release_block(),
        'echo "REACHED_END"',
    ])
    return _bash(script, env=env)


@pytest.mark.parametrize("body,label", [(FAIL_LOUD, "loud"), (FAIL_SILENT, "silent")])
def test_release_refuses_to_tag_when_the_probe_cannot_be_trusted(tmp_path, body, label):
    """The goal's third outcome. release.sh is the sole v-tagger, so this is
    the site where fail-open does the most damage."""
    repo = _real_repo(tmp_path)
    env = _stub_git(tmp_path, body)
    p = _run_release_block(str(repo), dry=0, env=env)
    assert p.returncode != 0, f"{label}: release must REFUSE, got rc=0"
    assert "REACHED_END" not in p.stdout
    assert "cannot verify the working tree is clean" in p.stderr, p.stderr


def test_release_distinguishes_probe_failure_from_a_dirty_tree(tmp_path):
    """A wedged git is not a dirty tree, and "commit or stash" is the wrong
    instruction for it — it sends an operator hunting for changes that do not
    exist. Both REFUSE; only the message differs."""
    repo = _real_repo(tmp_path)
    (repo / "untracked.txt").write_text("y\n", encoding="utf-8")
    dirty = _run_release_block(str(repo), dry=0)
    assert dirty.returncode != 0
    assert "commit or stash" in dirty.stderr, dirty.stderr

    env = _stub_git(tmp_path, FAIL_SILENT)
    broken = _run_release_block(str(repo), dry=0, env=env)
    assert broken.returncode != 0
    assert "commit or stash" not in broken.stderr, broken.stderr


def test_release_dry_run_still_reports_the_probe_failure(tmp_path):
    """Dry-run must not enforce, but it must not report a broken probe as a
    clean tree either — that is the same fail-open one layer up."""
    repo = _real_repo(tmp_path)
    env = _stub_git(tmp_path, FAIL_SILENT)
    p = _run_release_block(str(repo), dry=1, env=env)
    assert p.returncode == 0, p.stderr
    assert "REACHED_END" in p.stdout
    assert "probe FAILED" in p.stdout, p.stdout


# ============================================================ seed-verify ===

def test_seed_verify_no_longer_suppresses_the_status_exit_code():
    src = VERIFY.read_text(encoding="utf-8")
    live = [l for l in src.splitlines()
            if "status --porcelain" in l and not l.lstrip().startswith("#")]
    assert len(live) == 1, live
    assert "2>&1" in live[0] and "2>/dev/null" not in live[0], live[0]
    assert "STATUS_RC" in src, "exit code must be captured, not dropped"


# ================================================= no site left behind ======

@pytest.mark.parametrize("path", [PROMOTE, RELEASE, TRANSPLANT, VERIFY])
def test_no_named_site_still_sends_status_stderr_to_devnull(path):
    """The goal's grep check, as an executable assertion.

    Comment lines are excluded deliberately: all four files DESCRIBE the old
    form in their rationale, and a scan that counted its own documentation as
    the defect would go red on a healthy file — guard-1099, which this session
    already tripped once.
    """
    offenders = [
        f"{path.name}:{i}: {l.strip()}"
        for i, l in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "status --porcelain" in l
        and "2>/dev/null" in l
        and not l.lstrip().startswith("#")
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize("path", [PROMOTE, RELEASE, TRANSPLANT, VERIFY])
def test_scripts_remain_syntactically_valid(path):
    assert _bash(f'bash -n "{path}"').returncode == 0
