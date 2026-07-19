"""Tests for completed-not-committed-sweep.py (0).

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


# ── 0: zero-SHA goal-id commit resolution (blind-spot fix) ─────────
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
    """POSITIVE CONTROL (0): a completed framework goal with NO SHA
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
        g, NOW, {}, goalid_status={"": {"x": False}}) is None  # other goal


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
    # Collision-safe: the prefix  must NOT match "(0)".
    assert mod.resolve_shas_by_goal_id("g-115-260", [repo]) == []
    # A goal-id in no commit -> empty.
    assert mod.resolve_shas_by_goal_id("g-999-99", [repo]) == []
    # Empty/None goal-id is a safe no-op (never a bare '()' grep).
    assert mod.resolve_shas_by_goal_id("", [repo]) == []


# ── _fetch_origin — cross-box stale-ref fix (false-positive #2, 0) ──
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
