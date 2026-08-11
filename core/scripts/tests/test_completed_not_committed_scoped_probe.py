"""--goal scoping + --no-fetch for the close-time probe ().

g-115-3838 asked for a close-time ancestry check so branch-not-landed stops
recurring. The check ALREADY EXISTED: completed-not-committed-sweep.py's tier 2
asks "did everything reach the default branch?" via `git branch -r --contains`,
which is the branch-enumeration form the goal explicitly preferred over
PR-listing. What was missing was a way to ask it about ONE goal, cheaply, at
close time. These two flags are that, and nothing more — no ancestry logic is
reimplemented here.

WHY --no-fetch EXISTS, measured rather than assumed (cc-05):
    --goal g-115-1655 --min-age-minutes 0              -> 24,131 ms
    --goal g-115-1655 --min-age-minutes 0 --no-fetch   ->  1,027 ms
The fetch does NOT scale with --goal: it refreshes all 57 discovered repos
regardless of scope, and it dominates. 24s per close is disqualifying for a
per-close advisory; 1s is not.

--no-fetch is sound at close time and UNSOUND for the scheduled sweep, so it is
deliberately opt-in rather than a default. At close the agent has just pushed
FROM THIS BOX, so local origin/* refs already reflect that push. The refs a
fetch would add are OTHER boxes' pushes — exactly what the g-115-2660 cross-box
guard needs — so the 24h fleet sweep must keep fetching or a commit landed
elsewhere reads as local-only and false-positives.

The load-bearing property tested below is that --no-fetch skips the REFRESH
without making the probe VACUOUS: default_refs must still resolve for every
repo, because a check that returns clean by having nothing to check is worse
than no check at all (guard-1832 — subject absent from the target).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "completed-not-committed-sweep.py"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _import():
    spec = importlib.util.spec_from_file_location(
        "completed_not_committed_sweep_scoped", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["completed_not_committed_sweep_scoped"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(*args):
    """Invoke the real CLI. stderr is CAPTURED, never discarded — a crash here
    once masqueraded as a 23x speedup because the timing run sent stderr to
    /dev/null and the JSON parser found a '{' inside the traceback."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180)
    return proc


# ---------------------------------------------------------------------------
# --no-fetch must not crash, and must not be vacuous
# ---------------------------------------------------------------------------

def test_no_fetch_emits_valid_json_and_exits_clean():
    """Regression: the first --no-fetch build put PosixPath objects in the
    fetch_status dict, which json.dumps refuses ('keys must be str, int, float,
    bool or None, not PosixPath'). It failed only at OUTPUT time, so the run
    looked fast and successful until stderr was actually read."""
    proc = _run("--goal", "g-115-1655", "--min-age-minutes", "0",
                "--no-fetch", "--output", "json")
    assert "Traceback" not in proc.stderr, proc.stderr[-600:]
    assert proc.returncode == 0, proc.stderr[-600:]
    json.loads(proc.stdout)          # must parse, not merely contain a brace


def test_no_fetch_marks_every_repo_skipped_rather_than_dropping_it():
    """The skip must be VISIBLE per repo, not silent. A reader of the report has
    to be able to tell a skipped refresh from a repo that was never discovered."""
    proc = _run("--goal", "g-115-1655", "--min-age-minutes", "0",
                "--no-fetch", "--output", "json")
    assert proc.returncode == 0, proc.stderr[-600:]
    report = json.loads(proc.stdout)
    fetch_status = report.get("fetch_status") or {}
    repos = report.get("candidate_repos") or []
    assert repos, "no candidate repos discovered — probe would be vacuous"
    assert len(fetch_status) == len(repos)
    assert set(fetch_status.values()) == {"skipped_no_fetch"}


def test_no_fetch_still_resolves_default_refs_so_the_probe_is_not_vacuous():
    """THE load-bearing assertion. Tier 2 decides stranded-ness by comparing a
    SHA against the repo's default ref. If --no-fetch left default_refs empty,
    every goal would score clean for lack of anything to compare against — a
    manufactured all-clear, which is the failure mode this whole sweep exists to
    prevent (its own docstring records a run that reported '0 flagged' while
    eleven PRs sat unmerged for eight days)."""
    proc = _run("--goal", "g-115-1655", "--min-age-minutes", "0",
                "--no-fetch", "--output", "json")
    assert proc.returncode == 0, proc.stderr[-600:]
    report = json.loads(proc.stdout)
    repos = report.get("candidate_repos") or []
    default_refs = report.get("default_refs") or {}
    assert len(default_refs) == len(repos), (
        f"default_refs resolved for {len(default_refs)} of {len(repos)} repos — "
        "the probe cannot judge ancestry for the remainder")
    assert all(bool(v) for v in default_refs.values())


# ---------------------------------------------------------------------------
# --goal scoping
# ---------------------------------------------------------------------------

def _a_local_completed_goal_id():
    """Resolve a completed goal id from THIS deployment's own queue.

    g-001-368: this test used to pin `g-115-1655`, an UPSTREAM goal id. On a
    deployment whose queues hold no g-115-*, the scoping filter correctly
    narrows to zero and the assertion pins a population that cannot exist —
    measured `scanned == 0` here. Same family as the asp-115 hardcoding swept
    out of six scripts under g-001-273.

    Why this matters more than one red test: its sibling
    test_unknown_goal_id_scans_zero_rather_than_falling_back_to_the_fleet
    asserts a BOGUS id scans zero. With the pinned id also scanning zero, the
    pair collapsed into two tests measuring the same thing, and the suite lost
    the POSITIVE arm — it could still prove "unknown scans zero" but no longer
    that "known scans exactly one", which is the assertion distinguishing real
    scoping from a filter that always returns nothing. Both controls, or
    neither proves scoping.

    Resolving at runtime (option b of the goal) rather than building a tmp
    world (option a) is deliberate and narrower: `_run` shells out to the real
    sweep against the real repo set, so full hermeticity is a larger change
    touching every test in this file. That broader environment coupling is
    real and remains — this fixes only the deployment-specific ID pin.
    """
    import subprocess as _sp
    # BASH, not a bare "bash" argv[0]: on Windows the bare name resolves via
    # CreateProcess, which searches System32 BEFORE PATH and reaches the WSL
    # launcher — which sees the repo under /mnt/c, strips the env _paths.sh
    # needs, and can hang past the timeout. The pre-commit gate caught this
    # exact line on first write; the helper is the canonical fix ().
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bash_helpers import BASH
    for source, asp in (("agent", "asp-001"), ("world", "asp-009")):
        try:
            out = _sp.run(
                [BASH, str(PROJECT_ROOT / "core/scripts/aspirations-read.sh"),
                 "--source", source, "--id", asp],
                capture_output=True, text=True, timeout=60).stdout
            data = json.loads(out)
            record = data if isinstance(data, dict) else data[0]
            for g in (record.get("goals") or []):
                if g.get("status") == "completed" and g.get("id"):
                    return g["id"]
        except Exception:
            continue
    return None


def test_goal_scoping_narrows_the_population_to_one():
    import pytest
    gid = _a_local_completed_goal_id()
    if not gid:
        pytest.skip("no completed goal in this deployment's queues to scope to")
    proc = _run("--goal", gid, "--min-age-minutes", "0",
                "--no-fetch", "--output", "json")
    assert proc.returncode == 0, proc.stderr[-600:]
    assert json.loads(proc.stdout).get("scanned") == 1, (
        f"--goal {gid} should narrow the population to exactly that goal; "
        "if this is 0 the scoping filter is matching nothing, which the "
        "unknown-id sibling test cannot distinguish from correct behaviour")


def test_unknown_goal_id_scans_zero_rather_than_falling_back_to_the_fleet():
    """A typo'd goal id must scan NOTHING. Falling back to the full population
    would make a scoped probe silently fleet-wide — slow, and attributing other
    goals' strandedness to this close."""
    proc = _run("--goal", "g-000-00-does-not-exist", "--min-age-minutes", "0",
                "--no-fetch", "--output", "json")
    assert proc.returncode == 0, proc.stderr[-600:]
    report = json.loads(proc.stdout)
    assert report.get("scanned") == 0
    assert not (report.get("flagged") or [])


# ---------------------------------------------------------------------------
# REACHABLE RED — the goal's explicit requirement: prove tier 2 FIRES on a
# commit that is not an ancestor of the default branch, by construction rather
# than by inspection. Drives the same classify_stranded the scoped CLI reaches.
# ---------------------------------------------------------------------------


def _pr_record(state="OPEN", number=53, hours_old=143.9):
    """The shape probe_sha_pull_request REALLY emits — created_at with the
    trailing Z the GitHub API returns, NOT a precomputed age field.

    Written the wrong way first, deliberately recorded: the initial fixture
    carried `age_hours: 144.0`, which the code never reads. It parses
    `created_at`, so age came back None and classify_stranded correctly
    suppressed the entry as "age unknown" — my reachable-red failed against
    working code. That is guard-920 exactly ("replicate the literal production
    arg shape, not the contract-ideal one"), and the sibling fixture's own
    docstring warns about it one file over."""
    import datetime as _dt
    return {
        "state": state,
        "number": number,
        "url": f"https://github.com/example/repo/pull/{number}",
        "title": "fix: ship the thing (g-350-77)",
        "created_at": (_dt.datetime.now()
                       - _dt.timedelta(hours=hours_old)).isoformat(
                           timespec="seconds") + "Z",
    }



def _stranded_goal(mod, sha):
    import datetime as dt
    return {
        "id": "g-350-77",
        "status": "completed",
        "work_class": "product",
        "completed_at": (dt.datetime.now() - dt.timedelta(hours=2)).isoformat(
            timespec="seconds"),
        "_source": "world",
        "_aspiration_id": "asp-350",
        "title": "Ship the thing",
        "outcome_note": f"Committed {sha} and pushed to the feature branch.",
        "verification": {"summary": "suite green"},
    }


def test_reachable_red_offdefault_sha_with_aged_open_pr_is_flagged_stranded():
    """RED: SHA is on a remote branch but NOT on the default branch, carried by
    an open PR older than the age floor -> stranded_open_pr."""
    mod = _import()
    import datetime as dt
    sha = "abc1234"
    now = dt.datetime.now()
    goal = _stranded_goal(mod, sha)
    entry = mod.classify_stranded(
        goal, now,
        sha_status={sha: True},                       # landed on SOME remote branch
        default_status={sha: False},                  # but NOT on the default branch
        pr_status={sha: _pr_record("OPEN", hours_old=143.9)},
        min_age_minutes=0.0, lookback_hours=168.0, min_pr_age_hours=24.0)
    assert entry is not None, "tier 2 did NOT fire on an off-default SHA"
    assert entry["reason"] == "stranded_open_pr"


def test_reachable_green_same_sha_on_default_branch_is_not_flagged():
    """GREEN control for the red above — identical inputs except the SHA IS on
    the default branch. Without this pair the red proves only that the function
    returns something, not that it DISCRIMINATES."""
    mod = _import()
    import datetime as dt
    sha = "abc1234"
    goal = _stranded_goal(mod, sha)
    entry = mod.classify_stranded(
        goal, dt.datetime.now(),
        sha_status={sha: True},
        default_status={sha: True},                   # ON the default branch
        pr_status={sha: _pr_record("MERGED", hours_old=143.9)},
        min_age_minutes=0.0, lookback_hours=168.0, min_pr_age_hours=24.0)
    assert entry is None, f"flagged a landed commit as stranded: {entry}"
