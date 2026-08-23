"""test_completed_not_committed_worker_ref_probe.py — regression for 
outcome 3, against a REAL git fixture carrying a refs/workers/** ref.

WHAT WAS ALREADY COVERED, AND WHY IT WAS NOT ENOUGH. The fix landed in
9d71608fd and the sweep's existing suite exercises both neighbours of it:
`test_completed_not_committed_reachability.py` pins that the CLASSIFIER maps a
`STRANDED_WORKER_REF` verdict to `stranded_worker_ref`, and
`test_completed_not_committed_sweep.py` pins `_fetch_origin`'s per-repo
ok/failed/no-origin classification by monkeypatching `_git`. Neither one calls
`probe_sha_origin` — measured before writing this file, there were ZERO calls to
it anywhere under core/scripts/tests/. So the function that DECIDES whether a
sha reached origin, and the one the defect actually lived in, had its two
neighbours pinned and itself untested.

That gap is the shape the fix exists to correct, one layer up: a probe is only
as good as what it is allowed to READ, and monkeypatching `_git` replaces
exactly the layer where the blindness lived. `git branch -r --contains` walks
refs/remotes/** and nothing else, so refs/workers/<agent>/<sid> — the Mind/Body
architecture's normal delivery carrier — was structurally invisible to it. A
fake `_git` cannot reproduce that, because the thing being tested is what real
git does with a real refspec.

SO THESE FIXTURES USE REAL REPOS, following the convention proven in
`test_commit_reachability.py`: a real bare `origin`, a real work repo, real
pushes, real refs. Nothing is injected below the API, so the fetch refspec, the
ref enumeration and the containment reads are all genuinely exercised.

BOTH HALVES OF THE FIX, IN ONE PATH. 9d71608fd's own note calls it "two halves
that are one fix — either alone is inert": `_fetch_origin` must mirror
`+refs/workers/*:refs/workers/*` locally, and `probe_sha_origin` must then read
that mirror with `for-each-ref`. `test_worker_ref_sha_reads_as_landed` drives
them in that order against a sha pushed ONLY to origin's worker namespace, so
removing either half reddens it. `test_probe_is_blind_without_the_worker_fetch`
pins the halves' dependency directly, by probing before any fetch has run.

EXCLUDED, named rather than implied:
  * network transport — `origin` is a local bare repo, so an auth/DNS fetch
    failure is not reachable here.
  * `_fetch_origin`'s fail-open classification, already covered by
    `test_fetch_origin_classifies_and_fails_open` in the main sweep suite.

ANTI-VACUITY. `test_verdicts_are_distinct` proves the probe DISCRIMINATES
rather than returning one constant — without it every True-asserting case below
would pass against a function that returns True unconditionally, which is
precisely the mutation this file must not survive (guard-1793).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_worker_ref", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

# Reachable from no ref in any fixture, and not a valid object anywhere.
SHA_NONEXISTENT = "0123456789abcdef0123456789abcdef01234567"


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


def _build(tmp):
    """A work repo whose origin carries one sha in EACH delivery shape.

    Every non-main sha is created on its own branch off `landed`, so none of
    them is an ancestor of main — otherwise `branch -r --contains` would find
    them through origin/main and the worker-namespace read would never be
    reached, making the whole file vacuous.
    """
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

    # The regression case: pushed ONLY into origin's worker-carrier namespace.
    # This is what iteration-push.sh --push-worker-ref produces.
    _run(work, "checkout", "-q", "-b", "wk", shas["landed"])
    shas["worker"] = _commit(work, "worker-only.txt")
    _run(work, "push", "-q", "origin", "wk:refs/workers/fixture-agent/sid-0001")

    # An ordinary unmerged feature branch — landed, via refs/remotes.
    _run(work, "checkout", "-q", "-b", "feat", shas["landed"])
    shas["branch"] = _commit(work, "feature-only.txt")
    _run(work, "push", "-q", "origin", "feat")

    # Genuinely unpushed. The detector's whole reason to exist.
    _run(work, "checkout", "-q", "-b", "localonly", shas["landed"])
    shas["local"] = _commit(work, "local-only.txt")

    _run(work, "checkout", "-q", "main")
    return work, shas


@pytest.fixture(scope="module")
def fetched():
    """Fixture AFTER `_fetch_origin` has run — the production ordering.

    main() calls _fetch_origin(candidate_repos) once before probing, so this is
    the state every real probe sees.
    """
    tmp = tempfile.mkdtemp(prefix="cnc-workerref-")
    work, shas = _build(tmp)
    cnc._fetch_origin([work])
    return work, shas


@pytest.fixture(scope="module")
def unfetched():
    """Fixture with NO fetch run — isolates the second half of the fix."""
    tmp = tempfile.mkdtemp(prefix="cnc-workerref-nofetch-")
    return _build(tmp)


# --- the regression this goal exists for -----------------------------------

def test_worker_ref_sha_reads_as_landed(fetched):
    """A sha delivered ONLY on refs/workers/** is on origin, so the probe must
    say True. Before 9d71608fd it said False, and tier 1 filed a HIGH goal
    whose prescribed remedy was "Push the commit" — a no-op, because the commit
    was already there. Measured 2026-08-11: all three tier-1 flags in that lane
    were this one cause.
    """
    work, shas = fetched
    assert cnc.probe_sha_origin(shas["worker"], [work]) is True


def test_worker_ref_sha_is_invisible_to_the_remote_branch_read(fetched):
    """The mechanism, pinned separately from the outcome.

    `branch -r --contains` walks refs/remotes/** and nothing else. If this ever
    starts returning the worker sha, the fix's second read has become dead code
    and the test above would keep passing for the wrong reason.
    """
    work, shas = fetched
    rc, out = cnc._git(work, "branch", "-r", "--contains", shas["worker"])
    assert rc == 0 and not out.strip(), (
        "the worker sha became visible to `branch -r` — if that is a deliberate "
        "change, the namespace-agnostic read below is now redundant and this "
        f"file needs rewriting, not silencing. Got: {out!r}")

    rc2, out2 = cnc._git(work, "for-each-ref", "--contains", shas["worker"],
                         "refs/workers/")
    assert rc2 == 0 and out2.strip(), (
        "the worker sha is not under refs/workers/ locally — _fetch_origin's "
        "explicit refspec did not mirror it, so the probe is reading nothing")


def test_probe_is_blind_without_the_worker_fetch(unfetched):
    """The two halves are one fix: the read is inert without the mirror.

    With no fetch, origin's refs/workers/** was never copied locally, so
    `for-each-ref` finds nothing and the probe correctly degrades to the old
    answer. This is the fail-open contract stated in _fetch_origin's comment,
    and it is why adding the `for-each-ref` read ALONE would have fixed nothing.
    """
    work, shas = unfetched
    assert cnc.probe_sha_origin(shas["worker"], [work]) is False


# --- the other shapes must keep their existing answers ---------------------

def test_remote_branch_sha_reads_as_landed(fetched):
    work, shas = fetched
    assert cnc.probe_sha_origin(shas["branch"], [work]) is True


def test_local_only_sha_still_reads_as_unpushed(fetched):
    """The direction that matters. This sweep detects real deliverable loss
    (rb-3135 / g-115-2570); if a genuinely unpushed commit ever read as landed,
    the detector would stop detecting and the fix would have cost more than the
    defect.
    """
    work, shas = fetched
    assert cnc.probe_sha_origin(shas["local"], [work]) is False


def test_unknown_sha_reads_as_none(fetched):
    """None means "not a commit in any candidate repo" — distinct from False,
    because an unprobeable sha must not be reported as unpushed."""
    work, _ = fetched
    assert cnc.probe_sha_origin(SHA_NONEXISTENT, [work]) is None


def test_no_candidate_repos_reads_as_none(fetched):
    assert cnc.probe_sha_origin(SHA_NONEXISTENT, []) is None


# --- anti-vacuity ----------------------------------------------------------

def test_verdicts_are_distinct(fetched):
    """Proves the probe DISCRIMINATES. Without this, every True assertion above
    would survive a mutation that returns True unconditionally — and that
    mutation is exactly "declare everything landed", which silently disables
    the detector rather than breaking it (guard-1793).
    """
    work, shas = fetched
    verdicts = {
        "worker": cnc.probe_sha_origin(shas["worker"], [work]),
        "branch": cnc.probe_sha_origin(shas["branch"], [work]),
        "local": cnc.probe_sha_origin(shas["local"], [work]),
        "unknown": cnc.probe_sha_origin(SHA_NONEXISTENT, [work]),
    }
    assert set(verdicts.values()) == {True, False, None}, (
        f"probe returned a degenerate verdict set: {verdicts}")
