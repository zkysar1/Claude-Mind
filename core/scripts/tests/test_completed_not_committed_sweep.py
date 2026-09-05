"""Tests for completed-not-committed-sweep.py ().

The sweep flags code-deliverable goals closed status=completed whose commit is
absent from origin past a 30-min push-throttle window (rb-3135 completed!=
committed class). Detective only; these tests pin the pure eligibility ladder
and the two false-positive fixes the first live run forced:

  1. KEYWORD-ANCHORED SHA extraction — a hex token counts only next to a commit
     keyword or as a push-range endpoint. Free-floating hex (dates, env ids,
     message hashes) is NOT extracted. The first run flagged 196/2293 goals
     because UNANCHORED hex matched "20260711" (8 valid hex digits) etc.
  2. None-status DROP — classify_goal flags ONLY status=False SHAs (a real local
     commit validated by cat-file, on no remote). None-status tokens (not a real
     commit anywhere) are dropped, not flagged — they are the date/env-id noise.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the script
name has hyphens, so it cannot be a plain `import`). classify_goal takes an
INJECTED sha_status map, so the full ladder is unit-testable with zero git/daemon.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "completed-not-committed-sweep.py"

# Fixed reference time so age_hours is deterministic across machines.
NOW = dt.datetime(2026, 7, 18, 12, 0, 0)
# A completed_at 2 hours before NOW: past the 30-min throttle, inside lookback.
TWO_H_AGO = (NOW - dt.timedelta(hours=2)).isoformat(timespec="seconds")


def _import():
    spec = importlib.util.spec_from_file_location(
        "completed_not_committed_sweep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["completed_not_committed_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


def _goal(**kw):
    """Canonical completed code-deliverable goal naming ONE committed SHA;
    override fields via kwargs."""
    g = {
        "id": "g-350-99",
        "status": "completed",
        "work_class": "product",
        "completed_at": TWO_H_AGO,
        "_source": "world",
        "_aspiration_id": "asp-350",
        "title": "Fix: unit-tree root-field dual-read",
        "outcome_note": "Committed f885a690 and pushed to origin/main.",
        "verification": {"summary": "full suite green"},
    }
    g.update(kw)
    return g


# ── _parse_iso (guard-420 datetime tolerance) ──────────────────────────────

def test_parse_iso_valid_and_z_strip():
    mod = _import()
    assert mod._parse_iso("2026-07-18T10:00:00") == dt.datetime(2026, 7, 18, 10, 0, 0)
    assert mod._parse_iso("2026-07-18T10:00:00Z") == dt.datetime(2026, 7, 18, 10, 0, 0)


def test_parse_iso_empty_none_malformed_return_none():
    mod = _import()
    assert mod._parse_iso("") is None
    assert mod._parse_iso(None) is None
    assert mod._parse_iso("not-a-date") is None
    assert mod._parse_iso("2026-13-99") is None


# ── extract_commit_shas — KEYWORD-ANCHORED (false-positive fix #1) ──────────

def test_extract_anchored_commit_keywords():
    mod = _import()
    assert "f885a690" in mod.extract_commit_shas(
        _goal(outcome_note="Committed f885a690 to the repo."))
    assert "9cc1ce8f" in mod.extract_commit_shas(
        _goal(outcome_note="pushed 9cc1ce8f", verification={}))
    assert "d8296a44" in mod.extract_commit_shas(
        _goal(outcome_note="merged d8296a44 into main", verification={}))


def test_extract_origin_line():
    mod = _import()
    shas = mod.extract_commit_shas(
        _goal(outcome_note="origin/main now at abc1234ef", verification={}))
    assert "abc1234ef" in shas


def test_extract_push_range_both_endpoints():
    mod = _import()
    shas = mod.extract_commit_shas(
        _goal(outcome_note="pushed range 1111111..2222222", verification={}))
    assert "1111111" in shas and "2222222" in shas


def test_extract_ignores_unanchored_hex_dates_and_env_ids():
    """THE false-positive fix (196/2293 flood): bare hex NOT next to a commit
    keyword must NOT be extracted — dates ('20260711'), env ids ('a7cb5456'),
    session hashes."""
    mod = _import()
    g = _goal(
        outcome_note="Ran on 20260711 for env a7cb5456; session deadbeef1 completed.",
        verification={"summary": "no commit here"})
    assert mod.extract_commit_shas(g) == []


def test_extract_dedups_repeated_sha():
    mod = _import()
    g = _goal(outcome_note="committed abc1234 then pushed abc1234 again",
              verification={})
    assert mod.extract_commit_shas(g).count("abc1234") == 1


# ── is_code_deliverable ────────────────────────────────────────────────────

def test_code_deliverable_by_work_class():
    mod = _import()
    assert mod.is_code_deliverable(_goal(work_class="framework", outcome_note="",
                                         verification={})) is True
    assert mod.is_code_deliverable(_goal(work_class="product", outcome_note="",
                                         verification={})) is True


def test_code_deliverable_by_commit_keyword_in_evidence():
    mod = _import()
    g = _goal(work_class="hygiene", outcome_note="pushed the change to main",
              verification={})
    assert mod.is_code_deliverable(g) is True


def test_non_code_deliverable_skipped():
    """A knowledge/tree/journal-only close (no code lane, no commit keyword, no
    SHA) is NOT a code deliverable — never flag it for 'no commit'."""
    mod = _import()
    g = _goal(work_class="hygiene",
              outcome_note="Updated the knowledge tree node and journal.",
              verification={"summary": "tree edit only"})
    assert mod.is_code_deliverable(g) is False


# ── classify_goal — the eligibility ladder (INJECTED sha_status) ────────────

def test_committed_not_pushed_is_flagged():
    """Canonical incident: a real local commit (status False) on NO remote →
    flagged committed_not_pushed."""
    mod = _import()
    g = _goal(outcome_note="Committed abc1234 (push failed).", verification={})
    entry = mod.classify_goal(g, NOW, {"abc1234": False})
    assert entry is not None
    assert entry["goal_id"] == "g-350-99"
    assert entry["reason"] == "committed_not_pushed"
    assert entry["shas_absent_local_only"] == ["abc1234"]
    assert entry["age_hours"] == 2.0


def test_none_status_sha_is_dropped_not_flagged():
    """False-positive fix #2: a None-status token (not a real commit anywhere)
    is dropped — the date/env-id noise class. Not flagged."""
    mod = _import()
    g = _goal(outcome_note="Committed abc1234.", verification={})
    assert mod.classify_goal(g, NOW, {"abc1234": None}) is None


def test_landed_sha_is_clean():
    """Any SHA on a remote branch (status True) proves the deliverable shipped
    → clean, even if other SHAs are absent."""
    mod = _import()
    g = _goal(outcome_note="committed aaa1111 then pushed bbb2222", verification={})
    assert mod.classify_goal(g, NOW, {"aaa1111": False, "bbb2222": True}) is None


def test_non_completed_status_not_flagged():
    mod = _import()
    for st in ("pending", "in-progress", "blocked", "skipped"):
        g = _goal(status=st, outcome_note="committed abc1234", verification={})
        assert mod.classify_goal(g, NOW, {"abc1234": False}) is None


def test_fresh_close_inside_throttle_not_flagged():
    """A commit completed 10 min ago is inside the 30-min push-throttle window
    — cross-box push may still be pending. Not actionable yet."""
    mod = _import()
    ten_min_ago = (NOW - dt.timedelta(minutes=10)).isoformat(timespec="seconds")
    g = _goal(completed_at=ten_min_ago, outcome_note="committed abc1234",
              verification={})
    assert mod.classify_goal(g, NOW, {"abc1234": False}) is None


def test_too_old_outside_lookback_not_flagged():
    """A commit completed 8 days ago is past the 7-day lookback — bounded report."""
    mod = _import()
    eight_days_ago = (NOW - dt.timedelta(days=8)).isoformat(timespec="seconds")
    g = _goal(completed_at=eight_days_ago, outcome_note="committed abc1234",
              verification={})
    assert mod.classify_goal(g, NOW, {"abc1234": False}) is None


def test_no_sha_not_flagged():
    """A completed code deliverable that names no SHA is out of the false-
    positive-free SHA-probe core (never-committed is undetectable from prose)."""
    mod = _import()
    g = _goal(work_class="product",
              outcome_note="Did the work and pushed it.", verification={})
    assert mod.extract_commit_shas(g) == []
    assert mod.classify_goal(g, NOW, {}) is None


def test_missing_completed_at_not_flagged():
    mod = _import()
    g = _goal(completed_at=None, outcome_note="committed abc1234", verification={})
    assert mod.classify_goal(g, NOW, {"abc1234": False}) is None


def test_min_age_and_lookback_are_tunable():
    """Explicit thresholds override defaults — a 10-min-old close IS flagged at
    min_age_minutes=5, and an 8-day-old close IS flagged at lookback_hours=240."""
    mod = _import()
    ten_min_ago = (NOW - dt.timedelta(minutes=10)).isoformat(timespec="seconds")
    g1 = _goal(completed_at=ten_min_ago, outcome_note="committed abc1234",
               verification={})
    assert mod.classify_goal(g1, NOW, {"abc1234": False},
                             min_age_minutes=5.0) is not None
    eight_days_ago = (NOW - dt.timedelta(days=8)).isoformat(timespec="seconds")
    g2 = _goal(completed_at=eight_days_ago, outcome_note="committed abc1234",
               verification={})
    assert mod.classify_goal(g2, NOW, {"abc1234": False},
                             lookback_hours=240.0) is not None


# ── _existing_investigate (in-memory dedup) ────────────────────────────────

def test_existing_investigate_dedup():
    mod = _import()
    key = "investigate:completed-not-committed-g-350-99"
    all_goals = [{"origin_signal": key, "status": "pending"}]
    assert mod._existing_investigate("g-350-99", all_goals) is True
    # A resolved/skipped prior Investigate does NOT block re-filing.
    all_goals = [{"origin_signal": key, "status": "completed"}]
    assert mod._existing_investigate("g-350-99", all_goals) is False
    # No prior Investigate at all.
    assert mod._existing_investigate("g-350-99", []) is False


# ── : zero-SHA goal-id commit resolution (blind-spot fix) ─────────
# Loop-commit messages embed the goal-id, not a SHA (rb-3999), so the COMMON
# phantom record shape carries zero SHA tokens and the extracted-SHA path is
# structurally blind to it (both 2026-07-18 phantoms had zero SHA tokens).
# classify_goal takes an INJECTED goalid_status map so the fallback ladder is
# unit-testable with zero git.

def _zero_sha_goal(**kw):
    """A completed framework goal with NO SHA token anywhere in its record
    (the phantom shape). is_code_deliverable is satisfied by work_class."""
    g = _goal(work_class="framework",
              outcome_note="Closed deep; tree node reconciled.",
              verification={"summary": "all green"})
    g.update(kw)
    return g


def test_zero_sha_goalid_resolved_local_only_flags():
    """POSITIVE CONTROL (): a completed framework goal with NO SHA
    token, whose goal-id-resolved commit is local-only (status False), MUST be
    flagged. The exact class the g-115-2570 sweep shipped to catch but could not
    see."""
    mod = _import()
    g = _zero_sha_goal(id="g-115-9999")
    assert mod.extract_commit_shas(g) == []            # precondition: zero SHAs
    # Old behavior (no goalid_status injected) unchanged — still None.
    assert mod.classify_goal(g, NOW, {}) is None
    # New path: goal-id resolves to a local-only commit -> FLAG.
    entry = mod.classify_goal(g, NOW, {},
                              goalid_status={"g-115-9999": {"abc1234def": False}})
    assert entry is not None
    assert entry["goal_id"] == "g-115-9999"
    assert entry["reason"] == "committed_not_pushed"
    assert entry["resolved_via"] == "goal-id"
    assert entry["shas_absent_local_only"] == ["abc1234def"]
    assert entry["age_hours"] == 2.0


def test_zero_sha_goalid_resolved_landed_is_clean():
    """A goal-id-resolved commit ON origin (status True) is clean — the
    deliverable shipped, just without a SHA in the record."""
    mod = _import()
    g = _zero_sha_goal(id="g-115-9998")
    assert mod.classify_goal(
        g, NOW, {}, goalid_status={"g-115-9998": {"def5678abc": True}}) is None
    # Mixed: one landed proves the deliverable shipped even if another is local.
    assert mod.classify_goal(
        g, NOW, {},
        goalid_status={"g-115-9998": {"aaa": False, "bbb": True}}) is None


def test_zero_sha_no_goalid_commit_not_flagged():
    """PRECISION PRESERVED: a zero-SHA code goal with NO goal-id-resolved commit
    (git log --grep found nothing) is NOT flagged — a legitimate narrative/docs
    close is out of scope, never a false positive."""
    mod = _import()
    g = _zero_sha_goal(id="g-115-9997")
    assert mod.classify_goal(g, NOW, {}, goalid_status={}) is None
    assert mod.classify_goal(
        g, NOW, {}, goalid_status={"g-999-99": {"x": False}}) is None  # other goal


def test_zero_sha_goalid_none_status_dropped():
    """SYMMETRY with the SHA path: a goal-id-resolved token that is None-status
    (not a real commit anywhere) is dropped, not flagged — same false-positive
    guard as the extracted-SHA None-drop."""
    mod = _import()
    g = _zero_sha_goal(id="g-115-9996")
    assert mod.classify_goal(
        g, NOW, {}, goalid_status={"g-115-9996": {"notacommit": None}}) is None


def test_resolve_shas_by_goal_id_finds_tagged_commit(tmp_path):
    """INTEGRATION: a commit whose subject carries the iteration-commit scope
    '(<goal-id>)' is found by resolve_shas_by_goal_id; the parenthesized form is
    collision-safe (a prefix goal-id does NOT match a longer one)."""
    import subprocess
    mod = _import()
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    git("add", "-A")
    git("commit", "-q", "-m", "chore(g-115-2600): fix the sweep")

    shas = mod.resolve_shas_by_goal_id("g-115-2600", [repo])
    assert len(shas) == 1 and len(shas[0]) == 40      # one full-SHA match
    # Collision-safe: the prefix  must NOT match "()".
    assert mod.resolve_shas_by_goal_id("g-115-260", [repo]) == []
    # A goal-id in no commit -> empty.
    assert mod.resolve_shas_by_goal_id("g-999-99", [repo]) == []
    # Empty/None goal-id is a safe no-op (never a bare '()' grep).
    assert mod.resolve_shas_by_goal_id("", [repo]) == []


# ── _fetch_origin — cross-box stale-ref fix (false-positive #2, ) ──
# probe_sha_origin reads box-LOCAL origin/* refs via `git branch -r --contains`;
# without a prior `git fetch`, a commit landed on the remote from ANOTHER box
# (unfetched here) reads as local-only -> false-positive committed_not_pushed.
# main() now runs _fetch_origin(candidate_repos) once before build_sha_status to
# refresh those refs. These tests monkeypatch _git (the only side-effecting call)
# via save/restore so no real git/network is touched — they pin the per-repo
# classification and the fail-open contract.

def test_fetch_origin_classifies_and_fails_open():
    """Each repo is classified ok / failed / no-origin; a fetch that returns
    rc!=0 is recorded 'failed' and NEVER raised (fail-open) — else one offline
    repo would abort the whole sweep before any goal is probed (g-115-2660)."""
    mod = _import()
    orig_git = mod._git
    calls = []

    def fake_git(repo, *args, timeout=15):
        calls.append((str(repo), args))
        r = str(repo)
        if args[:1] == ("remote",):
            # "/repo/no-origin" has no origin remote configured; others do.
            return (0, "") if "no-origin" in r else (0, "origin\n")
        if args[:1] == ("fetch",):
            # "/repo/offline" fails the network fetch (rc=1); others succeed.
            return (1, "") if "offline" in r else (0, "")
        return (0, "")

    mod._git = fake_git
    try:
        result = mod._fetch_origin(["/repo/ok", "/repo/offline", "/repo/no-origin"])
    finally:
        mod._git = orig_git

    assert result == {
        "/repo/ok": "ok",
        "/repo/offline": "failed",     # recorded, not raised
        "/repo/no-origin": "no-origin",
    }
    # A no-origin repo is never fetched (no wasted network call, no spurious fail).
    assert not any(a[:1] == ("fetch",) for rp, a in calls if "no-origin" in rp)
    # The offline repo WAS attempted before being degraded to "failed".
    assert any(a[:1] == ("fetch",) for rp, a in calls if "offline" in rp)


def test_fetch_origin_uses_extended_timeout():
    """The fetch must pass a timeout longer than _git's 15s default — a real
    network `git fetch` routinely exceeds 15s, and a too-short timeout would
    manufacture the very 'failed' path the fix exists to avoid (g-115-2660)."""
    mod = _import()
    orig_git = mod._git
    seen = {}

    def fake_git(repo, *args, timeout=15):
        if args[:1] == ("remote",):
            return (0, "origin\n")
        if args[:1] == ("fetch",):
            seen["fetch_timeout"] = timeout
            return (0, "")
        return (0, "")

    mod._git = fake_git
    try:
        mod._fetch_origin(["/repo/ok"])
    finally:
        mod._git = orig_git
    assert seen.get("fetch_timeout", 15) > 15


# ── apply_superseded — benign convergent-parallel-fix orphan () ────

def test_apply_superseded_all_absent_superseded_marks_benign():
    """Every local-only SHA superseded-in-HEAD -> benign_superseded, suppressed
    from filing (deliverable present under a different SHA)."""
    mod = _import()
    entry = {"goal_id": "g-115-3031", "reason": "committed_not_pushed",
             "shas_absent_local_only": ["fcb8dd0", "aaa1111"]}
    out = mod.apply_superseded(entry, {"fcb8dd0": True, "aaa1111": True})
    assert out["benign_superseded"] is True
    assert out["reason"] == "benign_superseded"


def test_apply_superseded_one_not_superseded_keeps_flag():
    """>=1 local-only SHA NOT superseded -> real lost deliverable, stays flagged
    (conservative: one un-superseded SHA keeps the flag)."""
    mod = _import()
    entry = {"goal_id": "g-115-9999", "reason": "committed_not_pushed",
             "shas_absent_local_only": ["fcb8dd0", "bbb2222"]}
    out = mod.apply_superseded(entry, {"fcb8dd0": True, "bbb2222": False})
    assert out["benign_superseded"] is False
    assert out["reason"] == "committed_not_pushed"


def test_apply_superseded_empty_status_keeps_flag():
    """Backward compat — no superseded info (empty map) -> not benign, stays
    flagged exactly as pre-g-115-3032."""
    mod = _import()
    entry = {"goal_id": "g-115-9998", "reason": "committed_not_pushed",
             "shas_absent_local_only": ["fcb8dd0"]}
    out = mod.apply_superseded(entry, {})
    assert out["benign_superseded"] is False
    assert out["reason"] == "committed_not_pushed"


def test_apply_superseded_no_absent_shas_not_benign():
    """No local-only SHAs -> bool(absent) False -> never benign (guards the
    all([]) == True vacuous-truth trap)."""
    mod = _import()
    entry = {"goal_id": "g-115-9997", "reason": "committed_not_pushed",
             "shas_absent_local_only": []}
    out = mod.apply_superseded(entry, {"whatever": True})
    assert out["benign_superseded"] is False


def test_sha_superseded_identical_files_true():
    """cat-file ok + changed files + `diff --quiet <sha> HEAD -- files` exit 0
    (identical) -> superseded True."""
    mod = _import()
    orig = mod._git

    def fake_git(repo, *args, timeout=15):
        if args[:2] == ("cat-file", "-e"):
            return (0, "")
        if args[:2] == ("diff", "--name-only"):
            return (0, "core/scripts/x.py\ncore/scripts/y.py")
        if args[:2] == ("diff", "--quiet"):
            return (0, "")  # identical in HEAD
        return (1, "")

    mod._git = fake_git
    try:
        assert mod.sha_superseded("fcb8dd0", ["/repo"]) is True
    finally:
        mod._git = orig


def test_sha_superseded_differing_files_false():
    """`diff --quiet` exit 1 (>=1 file differs/absent in HEAD) -> not superseded,
    keep the flag (real lost deliverable candidate)."""
    mod = _import()
    orig = mod._git

    def fake_git(repo, *args, timeout=15):
        if args[:2] == ("cat-file", "-e"):
            return (0, "")
        if args[:2] == ("diff", "--name-only"):
            return (0, "core/scripts/x.py")
        if args[:2] == ("diff", "--quiet"):
            return (1, "")  # differs from HEAD
        return (1, "")

    mod._git = fake_git
    try:
        assert mod.sha_superseded("fcb8dd0", ["/repo"]) is False
    finally:
        mod._git = orig


def test_sha_superseded_not_in_any_repo_false():
    """SHA in no candidate repo (cat-file fails everywhere) -> False (cannot
    check -> keep the flag, conservative)."""
    mod = _import()
    orig = mod._git

    def fake_git(repo, *args, timeout=15):
        return (1, "")  # cat-file -e fails in every repo

    mod._git = fake_git
    try:
        assert mod.sha_superseded("deadbee", ["/repo1", "/repo2"]) is False
    finally:
        mod._git = orig


def test_sha_superseded_root_or_merge_parent_error_false():
    """`diff --name-only <sha>^ <sha>` error (root commit / bad parent) -> False
    (cannot determine changed files -> keep the flag)."""
    mod = _import()
    orig = mod._git

    def fake_git(repo, *args, timeout=15):
        if args[:2] == ("cat-file", "-e"):
            return (0, "")
        if args[:2] == ("diff", "--name-only"):
            return (128, "")  # parent-resolve error (root commit)
        return (1, "")

    mod._git = fake_git
    try:
        assert mod.sha_superseded("r00tc0m", ["/repo"]) is False
    finally:
        mod._git = orig


# ── : TIER 2 — stranded on an unmerged branch ────────────────────
# Tier 1 decides "landed" with `git branch -r --contains`, which ANY remote
# branch satisfies. So work pushed to a feature branch whose pull request was
# never merged scored LANDED and CLEAN. That is not a theoretical hole: on
# 2026-07-23 a fleet-wide run reported "0 flagged — every completed goal's work
# landed in git/origin" while the oldest of eleven open Lodestar pull requests
# had been unmerged for eight days. A gate that is merely absent emits no
# signal; this one emitted a positive all-clear for invisible work.
#
# Fixture shapes are taken from the LIVE records the fix was measured against,
# not from an abstraction of them (sig-38: author detector predicates FROM the
# motivating incidents; guard-920: replicate the production shape, not the
# contract-ideal one). Both were verified against the real forge on 2026-07-27:
# PR #53 head bravo/-watch-csp-stale-cap carries exactly 3b0b14ee...,
# PR #54 head feat/-knowledge-node-body carries exactly 495fa814....

def _pr(state="OPEN", number=53, hours_old=143.9, **kw):
    """A pull-request record in the shape probe_sha_pull_request really emits.
    created_at carries the trailing Z the GitHub API actually returns (guard-920:
    replicate the production shape, not the contract-ideal one) — a naive-ISO
    fixture would exercise a parse path the sweep never takes and would hide a
    tz-handling regression in the PR-age gate."""
    rec = {
        "state": state,
        "number": number,
        "url": f"https://github.com/zkysar1/Vinheim-Web-App/pull/{number}",
        "title": "fix(watch): CSP allow :443 ALB watchUrl (g-335-190)",
        "created_at": (NOW - dt.timedelta(hours=hours_old)).isoformat(
            timespec="seconds") + "Z",
    }
    rec.update(kw)
    return rec


# The  shape: a MULTI-REPO goal whose record names one commit that
# reached main in an unprotected repo, while its other half sits on the branch
# of an open PR and is discoverable ONLY by goal-id commit-scope match.
_ON_DEFAULT = "326bf09"
_OFF_DEFAULT = "3b0b14ee08c4d2e734790f924a13a93e5fe1a50a"


def _multi_repo_goal(**kw):
    g = _goal(id="g-335-190",
              work_class="product",
              title="Live watch validation for PEARL - make 'watch it think' work",
              outcome_note=f"Committed {_ON_DEFAULT} and pushed.",
              verification={"summary": "watch page verified live"})
    g.update(kw)
    return g


def test_landed_shas_unions_record_and_goalid_paths():
    """REGRESSION GUARD for the false clean this lane shipped with. landed_shas
    must UNION both attribution paths, not pick one. classify_goal's either/or
    is right for tier 1 (one landing proves shipment) and WRONG here: with
    either/or, g-335-190's record-named on-default SHA wins, the goal-id-resolved
    off-default half is never looked at, and a goal with a 6-day-old open PR
    scores clean. Caught only by running the fixed sweep against the live
    estate — the unit tests and the tier-1 suite were both green."""
    mod = _import()
    g = _multi_repo_goal()
    landed = mod.landed_shas(
        g, NOW, {_ON_DEFAULT: True},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
    assert set(landed) == {_ON_DEFAULT, _OFF_DEFAULT}


def test_landed_shas_dedups_sha_reachable_both_ways():
    """A SHA named in the record AND resolved by goal-id appears once."""
    mod = _import()
    g = _multi_repo_goal()
    landed = mod.landed_shas(
        g, NOW, {_ON_DEFAULT: True},
        goalid_status={"g-335-190": {_ON_DEFAULT: True}})
    assert landed == [_ON_DEFAULT]


def test_landed_shas_excludes_non_landed_and_ineligible():
    """Only remote-landed SHAs count, and an ineligible goal yields nothing."""
    mod = _import()
    g = _multi_repo_goal()
    assert mod.landed_shas(g, NOW, {_ON_DEFAULT: False}) == []
    assert mod.landed_shas(_multi_repo_goal(status="in-progress"), NOW,
                           {_ON_DEFAULT: True}) == []


def test_stranded_open_pr_flags_multi_repo_partial_landing():
    """POSITIVE CONTROL, live shape: half the deliverable on the default branch,
    half on the branch of an open 6-day-old PR -> stranded_open_pr."""
    mod = _import()
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW,
        {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr()},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
    assert entry is not None
    assert entry["reason"] == "stranded_open_pr"
    assert entry["shas_off_default"] == [_OFF_DEFAULT]
    assert entry["pull_request"]["number"] == 53
    assert entry["resolved_via"] == "goal-id"


def test_stranded_clean_when_everything_reached_default():
    """NEGATIVE CONTROL: the ordinary shipped goal. Every landed SHA is on the
    default branch -> silent. 3,630 of 3,632 live goals took this path."""
    mod = _import()
    assert mod.classify_stranded(
        _multi_repo_goal(), NOW,
        {_ON_DEFAULT: True},
        {_ON_DEFAULT: True},
        {}) is None


def test_stranded_fresh_pr_is_in_flight_not_stranded():
    """A PR younger than min_pr_age_hours suppresses the entry ENTIRELY rather
    than demoting it to stranded_no_pr — freshly-opened work is in flight. Two
    live goals (g-335-268, g-335-45) sat in exactly this state and correctly
    produced no flag."""
    mod = _import()
    assert mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr(hours_old=3.0)},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}},
        min_pr_age_hours=24.0) is None


def test_stranded_unavailable_forge_never_flags():
    """The goal's explicit scope note: an unreachable forge must not turn a
    clean sweep into a flagged one. UNAVAILABLE degrades to silence."""
    mod = _import()
    assert mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: dict(mod._PR_UNAVAILABLE)},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}}) is None


def test_stranded_undeterminable_default_branch_never_flags():
    """Conservative in the NO-FLAG direction: if the default branch could not be
    resolved (None), we cannot claim the commit missed it."""
    mod = _import()
    assert mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: None, _OFF_DEFAULT: None},
        {}) is None


def test_stranded_no_pr_is_the_weaker_class():
    """Off-default with no pull request at all gets its own report-only class
    rather than a flag. NOT because it "could be a live working branch" — this
    sweep's population is already filtered to goals closed status=completed, so
    that reading does not survive. The measured reason (the bucket is dominated
    by worker-carrier refs that already have an owner) lives with the
    disposition in completed-not-committed-sweep.py; g-115-7704."""
    mod = _import()
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: {"state": "NONE", "number": None, "url": None,
                        "title": None, "created_at": None}},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
    assert entry["reason"] == "stranded_no_pr"
    assert entry["pull_request"] is None


def test_stranded_merged_into_non_default_base_is_weaker_class():
    """A MERGED PR whose commit still is not on the default branch merged into
    some other base. Rare and ambiguous -> report-only, PR record retained."""
    mod = _import()
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr(state="MERGED", number=99)},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
    assert entry["reason"] == "stranded_no_pr"
    assert entry["pull_request"]["number"] == 99


def test_tiers_are_mutually_exclusive():
    """classify_goal flags when NOTHING landed; classify_stranded looks only
    where something DID. No goal can be in both lanes, so main() needs no
    cross-dedup — this test is what makes that claim checkable."""
    mod = _import()
    g = _goal()  # names one SHA, local-only -> tier 1's lane
    assert mod.classify_goal(g, NOW, {"f885a690": False}) is not None
    assert mod.classify_stranded(
        g, NOW, {"f885a690": False}, {"f885a690": False}, {}) is None


def test_stranded_respects_goal_age_gates():
    """The existing completion-age discipline still bounds tier 2."""
    mod = _import()
    fresh = _multi_repo_goal(
        completed_at=(NOW - dt.timedelta(minutes=5)).isoformat(
            timespec="seconds"))
    assert mod.classify_stranded(
        fresh, NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr()},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}}) is None
    old = _multi_repo_goal(
        completed_at=(NOW - dt.timedelta(days=30)).isoformat(
            timespec="seconds"))
    assert mod.classify_stranded(
        old, NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr()},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}}) is None


def test_stranded_dedup_key_is_independent_of_tier_one():
    """The two lanes prescribe DIFFERENT remedies — push the commit vs merge the
    pull request — so a tier-1 Investigate must not suppress a tier-2 one."""
    mod = _import()
    tier1 = [{"origin_signal": "investigate:completed-not-committed-g-335-190",
              "status": "pending"}]
    assert mod._existing_investigate("g-335-190", tier1) is True
    assert mod._existing_investigate(
        "g-335-190", tier1, mod.STRANDED_SIGNAL_PREFIX) is False
    tier2 = [{"origin_signal": f"{mod.STRANDED_SIGNAL_PREFIX}g-335-190",
              "status": "pending"}]
    assert mod._existing_investigate(
        "g-335-190", tier2, mod.STRANDED_SIGNAL_PREFIX) is True
    assert mod._existing_investigate("g-335-190", tier2) is False


def test_file_investigate_body_names_the_pull_request():
    """The remedy is only actionable if the Investigate names the PR to merge."""
    mod = _import()
    captured = {}

    def fake_add(asp_id, body, source=None):
        captured["asp_id"] = asp_id
        captured["body"] = body
        return {"id": "g-115-9001"}

    orig = mod._rt.aspirations_add_goal
    mod._rt.aspirations_add_goal = fake_add
    try:
        entry = mod.classify_stranded(
            _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
            {_ON_DEFAULT: True, _OFF_DEFAULT: False},
            {_OFF_DEFAULT: _pr()},
            goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
        assert mod._file_investigate(entry) == "g-115-9001"
    finally:
        mod._rt.aspirations_add_goal = orig
    # The escalation aspiration is RESOLVED, not hardcoded ( built
    # _escalation_target;  swept the call sites). This assertion pinned
    # the pre-resolver literal and failed on every deployment where asp-115 does
    # not exist — i.e. downstream, where it resolves to asp-001. Assert the CONTRACT
    # (a non-empty id that the module actually resolved) rather than a constant
    # that is correct in exactly one deployment.
    assert captured["asp_id"] == mod.ESCALATION_ASP
    assert captured["asp_id"]
    body = captured["body"]
    assert "#53" in body["title"]
    assert body["origin_signal"] == f"{mod.STRANDED_SIGNAL_PREFIX}g-335-190"
    assert "pull/53" in body["description"]
    assert "default branch" in body["description"]


def test_probe_sha_on_default_error_is_undeterminable_not_a_flag():
    """`branch -r --contains` is used instead of `merge-base --is-ancestor`
    precisely so a probe ERROR (rc!=0) stays distinguishable from a genuine
    negative (rc==0, empty stdout). Errors must not become flags."""
    mod = _import()
    orig = mod._git

    def fake_git(repo, *args, timeout=15):
        if args[:2] == ("cat-file", "-e"):
            return (0, "")
        return (129, "")  # probe error

    mod._git = fake_git
    try:
        assert mod.probe_sha_on_default(
            "deadbee", ["/repo"], {"/repo": "origin/main"}) is None
    finally:
        mod._git = orig


def test_probe_sha_on_default_unknown_default_ref_is_undeterminable():
    """resolve_default_ref returning None must not be guessed around."""
    mod = _import()
    orig = mod._git

    def fake_git(repo, *args, timeout=15):
        return (0, "") if args[:2] == ("cat-file", "-e") else (1, "")

    mod._git = fake_git
    try:
        assert mod.probe_sha_on_default(
            "deadbee", ["/repo"], {"/repo": None}) is None
    finally:
        mod._git = orig


def test_probe_sha_pull_request_gh_failure_is_unavailable():
    """gh absent / unauthenticated / non-forge remote -> UNAVAILABLE, which
    classify_stranded maps to silence. Never an exception, never a flag."""
    mod = _import()
    orig_git, orig_gh = mod._git, mod._gh

    def fake_git(repo, *args, timeout=15):
        return (0, "") if args[:2] == ("cat-file", "-e") else (1, "")

    mod._git = fake_git
    mod._gh = lambda repo, *a, **k: (1, "")
    try:
        assert mod.probe_sha_pull_request("deadbee", ["/repo"])["state"] == \
            "UNAVAILABLE"
    finally:
        mod._git, mod._gh = orig_git, orig_gh


def _pr_probe_with(payload):
    """Run probe_sha_pull_request against a canned /pulls payload."""
    mod = _import()
    orig_git, orig_gh = mod._git, mod._gh

    def fake_git(repo, *args, timeout=15):
        return (0, "") if args[:2] == ("cat-file", "-e") else (1, "")

    mod._git = fake_git
    mod._gh = lambda repo, *a, **k: (0, json.dumps(payload))
    try:
        return mod.probe_sha_pull_request("deadbee", ["/repo"])
    finally:
        mod._git, mod._gh = orig_git, orig_gh


# INVERTED 2026-09-05 (). This test previously asserted the OPEN one
# wins, on the premise "when several PRs carry the commit, the OPEN one is the
# stranding". That premise is FALSE and the test was pinning the defect. The
# /pulls endpoint returns every PR whose head CONTAINS the sha, and on a split
# repo every branch cut from `dev` contains everything already merged into dev
# — so an unrelated open PR carries every recently-merged commit. Measured on
# zkysar1/Vinheim-Web-App: open PR #439 was named for four unrelated commits
# whose real merges were #426/#427/#429/#430 (guard-6045). The old fixture below
# is unchanged, because it was always the false-positive shape: #40 is MERGED.
def test_probe_sha_pull_request_prefers_the_merged_one():
    """A MERGED PR carrying the commit outranks an OPEN one that merely
    contains it — the merge is conclusive evidence the work shipped."""
    rec = _pr_probe_with([
        {"number": 40, "state": "closed", "merged_at": "2026-07-01T00:00:00Z",
         "html_url": "u40", "title": "t40", "created_at": "2026-06-30T00:00:00Z"},
        {"number": 53, "state": "open", "merged_at": None,
         "html_url": "u53", "title": "t53", "created_at": "2026-07-21T09:15:50Z"},
    ])
    assert rec["state"] == "MERGED" and rec["number"] == 40


def test_probe_sha_pull_request_still_prefers_open_when_nothing_merged():
    """POSITIVE CONTROL for the test above: the fix must not hardcode MERGED.

    With no merged PR carrying the sha, the OPEN one is still selected — that
    is the genuine stranding the sweep exists to flag, and inverting the
    preference must not blind it. Without this control, a fix that always
    returned norms[0] would pass the test above."""
    rec = _pr_probe_with([
        {"number": 41, "state": "closed", "merged_at": None,
         "html_url": "u41", "title": "t41", "created_at": "2026-06-30T00:00:00Z"},
        {"number": 54, "state": "open", "merged_at": None,
         "html_url": "u54", "title": "t54", "created_at": "2026-07-21T09:15:50Z"},
    ])
    assert rec["state"] == "OPEN" and rec["number"] == 54


def test_probe_sha_pull_request_empty_list_is_none_not_unavailable():
    """A successful query returning no PRs is genuine absence (NONE), which is a
    weaker signal — distinct from an UNAVAILABLE forge, which is no signal."""
    mod = _import()
    orig_git, orig_gh = mod._git, mod._gh

    def fake_git(repo, *args, timeout=15):
        return (0, "") if args[:2] == ("cat-file", "-e") else (1, "")

    mod._git = fake_git
    mod._gh = lambda repo, *a, **k: (0, "[]")
    try:
        assert mod.probe_sha_pull_request("deadbee", ["/repo"])["state"] == "NONE"
    finally:
        mod._git, mod._gh = orig_git, orig_gh


# ──  fresh-eyes pass: three defects the author-pass missed ────────
# Found by /fresh-eyes-code on the same code 40 minutes after writing it, each
# confirmed by probe rather than by reading. All three point the same way — the
# lane documents itself as conservative in the NO-FLAG direction and each defect
# leaned the other way.

def test_missing_pr_status_key_is_unavailable_not_absent_pr():
    """DEFECT 1. An off-default SHA with NO pr_status entry must be treated as
    UNAVAILABLE (silence), never as {} — which reads as "no PR exists" and lands
    the goal in stranded_no_pr. "Not probed" and "probe failed" are the same
    epistemic state; only one of them was being honoured. Latent while main()
    probes every off-default SHA, but the obvious optimization (narrow the gh
    probe set to bound network calls) would silently turn unprobed SHAs into
    report lines asserting a negative nobody measured."""
    mod = _import()
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {},  # probe set never covered _OFF_DEFAULT
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
    assert entry is None


def test_all_stranding_prs_are_reported_not_just_the_first():
    """DEFECT 2. A multi-repo goal can be stranded on SEVERAL open PRs at once.
    Naming one sends the reader to half the remedy. Live case: g-335-191 is
    stranded on BOTH Vinheim #54 and Zak-Code #129, and the first live report
    named only #54."""
    mod = _import()
    second = "cdc1c2a149855ba500a013df3c6eb24f9447b748"
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False, second: False},
        {_OFF_DEFAULT: _pr(number=54), second: _pr(number=129)},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True, second: True}})
    assert entry["reason"] == "stranded_open_pr"
    reported = {entry["pull_request"]["number"]} | {
        o["number"] for o in entry["other_pull_requests"]}
    assert reported == {54, 129}


def test_other_pull_requests_is_disjoint_from_the_primary():
    """other_pull_requests carries the REMAINDER, not a copy — one PR must not
    appear twice across the two fields (single source of truth)."""
    mod = _import()
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr(number=54)},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}})
    assert entry["pull_request"]["number"] == 54
    assert entry["other_pull_requests"] == []


def test_open_pr_with_unparseable_created_at_does_not_bypass_the_age_gate():
    """DEFECT 3, the worst of the three — the only one that produced a WRITE.
    `pr_age_hours is not None and pr_age_hours < min_pr_age_hours` let a PR whose
    created_at would not parse walk straight past the age gate into
    stranded_open_pr, which --apply files as an Investigate. "Cannot confirm the
    PR is old enough" is not "the PR is old enough"."""
    mod = _import()
    entry = mod.classify_stranded(
        _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
        {_ON_DEFAULT: True, _OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr(number=99, created_at=None)},
        goalid_status={"g-335-190": {_OFF_DEFAULT: True}},
        min_pr_age_hours=24.0)
    assert entry is None


def test_file_investigate_names_every_stranding_pr():
    """The Investigate body must name the extras too — merging only the PR in the
    title leaves the rest of the goal invisible, which is the exact failure this
    whole lane exists to surface."""
    mod = _import()
    captured = {}
    orig = mod._rt.aspirations_add_goal
    mod._rt.aspirations_add_goal = lambda a, b, source=None: (
        captured.update({"body": b}) or {"id": "g-115-9002"})
    try:
        second = "cdc1c2a149855ba500a013df3c6eb24f9447b748"
        entry = mod.classify_stranded(
            _multi_repo_goal(), NOW, {_ON_DEFAULT: True},
            {_ON_DEFAULT: True, _OFF_DEFAULT: False, second: False},
            {_OFF_DEFAULT: _pr(number=54), second: _pr(number=129)},
            goalid_status={"g-335-190": {_OFF_DEFAULT: True, second: True}})
        mod._file_investigate(entry)
    finally:
        mod._rt.aspirations_add_goal = orig
    desc = captured["body"]["description"]
    assert "#54" in desc and "#129" in desc


# ── git evidence admits terse closes () ──────────────────────────
#
# is_code_deliverable's three signals are all PROSE-shaped: work_class, a commit
# keyword somewhere in the evidence fields, or a SHA-looking token. Whether a
# goal produced code is a fact about git, not about how its closer narrated it,
# so a tersely-closed goal was skipped by BOTH tiers before any git resolution
# ran — and because the resolver was itself gated on the predicate, a goal it
# rejected could never acquire the evidence that would have admitted it.
#
# Measured on the live queue 2026-07-28: of the 136 in-window completed goals
# the predicate rejected, 127 (93%) had commits resolvable by goal-id. Three of
# them sat on branches of OPEN PRs ( -> Vinheim #55,  ->
# Vinheim #57,  ->  #162) and were invisible.

def _terse_goal(**kw):
    """The measured counter-example shape: completed, work_class 'unclassified',
    no outcome_note / completion_summary / verify_summary, and no commit keyword
    or SHA token anywhere. is_code_deliverable MUST return False for this."""
    g = _goal()
    for k in ("outcome_note", "verification"):
        g.pop(k, None)
    g.update({
        "id": "g-999-01",
        "work_class": "unclassified",
        "title": "did the thing",
        "description": "did the thing",
    })
    g.update(kw)
    return g


def test_terse_goal_is_rejected_by_the_prose_predicate():
    """Precondition for the tests below — if this ever starts returning True the
    fixture has drifted and the widening tests would pass vacuously."""
    mod = _import()
    assert mod.is_code_deliverable(_terse_goal()) is False


def test_git_evidence_admits_a_goal_the_prose_predicate_rejects():
    """Tier 1. Same goal, same prose; the ONLY difference is that its id resolved
    to a real commit."""
    mod = _import()
    g = _terse_goal()
    assert mod.classify_goal(g, NOW, {}, goalid_status={}) is None
    flagged = mod.classify_goal(
        g, NOW, {}, goalid_status={"g-999-01": {"a" * 40: False}})
    assert flagged is not None and flagged["goal_id"] == "g-999-01"


def test_no_git_evidence_still_skips_a_knowledge_only_close():
    """THE control that keeps the widening honest. The predicate exists so a
    docs/tree/journal-only close is never flagged for 'no commit'. Admitting on
    git evidence must not weaken that: no commit, still skipped."""
    mod = _import()
    g = _terse_goal()
    assert mod.classify_goal(g, NOW, {}, goalid_status={}) is None
    assert mod.classify_goal(g, NOW, {}, goalid_status=None) is None


def test_has_git_evidence_is_pure_and_reads_only_the_resolved_map():
    """It must not shell out — the git work belongs to build_goalid_status, and
    duplicating it here would put a `git log` inside the pure tier predicates."""
    mod = _import()
    g = _terse_goal()
    assert mod.has_git_evidence(g, None) is False
    assert mod.has_git_evidence(g, {}) is False
    assert mod.has_git_evidence(g, {"other-id": {"x": True}}) is False
    assert mod.has_git_evidence(g, {"g-999-01": {"a" * 40: True}}) is True
    # An empty per-goal map means "resolved nothing" -> not evidence.
    assert mod.has_git_evidence(g, {"g-999-01": {}}) is False
