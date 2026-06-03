"""test_attribution_filter_no_self_inflight.py — regression test for 2.

Pins the contract of core/scripts/_cross_agent_attribution_filter.py when the
SELF agent has NO in_flight claim (self.in_flight=null → self_claimed_at=0).

WHY this is the dominant runtime case, not an edge case (g-115-1154 finding):
team-state-clear-in-flight runs in iteration-close.sh do_verify Step 3
(line ~390, g-284-06) BEFORE post-state-update-gate.sh fires the filter in
do_state_update Step 8.78 (line ~809). So at filter time during a normal
iteration close, self.in_flight has ALREADY been cleared. self_claimed_at=0 is
the rule, not the exception.

Contract under self.in_flight=null (verified by reading filter_paths, not
inferred):
  - Graceful: filter_paths does not raise — `or {}` guards + _iso_to_epoch(None)=0.
  - Source 2 (partner uncommitted-edits log) STILL drops: it never reads
    self_claimed_at, so an explicit partner authorship record is honored
    regardless of self.in_flight. This is one of the two existing fallbacks
    that survive self.in_flight=null.
  - Source 1 (concurrent-partner via partner claim epoch) STILL drops: with
    self_claimed_at=0, skip_concurrent is False, so a file whose mtime is
    at/after a partner's claim is still attributed to that partner. The second
    existing fallback.
  - Source 3 (pre-claim-mtime) is DISABLED: its `self_claimed_at > 0` guard is
    False, so a partner file with an OLD mtime, no uncommitted-log entry, and
    no concurrent partner claim is KEPT. This is the documented fail-open gap
    (bias: never silently drop self-authored work; the cost is some pre-claim
    partner WIP at neutral paths leaks through when there is no self anchor).

REFRAME (g-115-1182): the originating sq-018 spark proposed a hypothetical
"Source 4 fallback (last_active / session-start / commit-time anchor)" to make
the filter drop partner files when self.in_flight=null. No Source 4 exists, and
adding one is NOT the team's chosen fix layer — the gap is being addressed at
the gate-scope layer (g-115-1178: scope post-state-update-gate to commit_sha)
and the path layer (g-115-1180: normalize uncommitted-edits.jsonl paths so
Source 2 catches more). This test therefore pins "partner files are STILL
dropped when self.in_flight=null" via the EXISTING Sources 1 and 2 (the real
fallbacks), and documents the Source 3 gap as a known limitation tracked by
g-115-1154 / g-115-1178 / g-115-1180 — NOT a desired end-state. If a Source 4
or gate-scope fix later changes Case 4's KEPT outcome, update Case 4 to assert
dropped.

Reuses the staging-helper pattern from test_post_state_update_attribution.py
(g-115-714 / g-115-741).

Run: py -3 -m pytest core/scripts/tests/test_attribution_filter_no_self_inflight.py -v
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _cross_agent_attribution_filter import filter_paths  # noqa: E402


# ── Test staging helpers (mirror test_post_state_update_attribution.py) ──────


def _stage_project(tmp_path, self_agent, partners):
    """Build a minimal PROJECT_ROOT with agent dirs + world dir.

    Phase 2.5.D layout: agent dirs live under PROJECT_ROOT/agents/<name>/.
    Returns (project_root, world_dir).
    """
    project_root = tmp_path / "repo"
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    for agent in [self_agent, *partners]:
        adir = project_root / "agents" / agent
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=" + world.as_posix() + "\n", encoding="utf-8"
        )
        (adir / "session").mkdir(parents=True, exist_ok=True)
    return project_root, world


def _write_team_state(world, agent_status):
    import yaml

    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": agent_status}), encoding="utf-8"
    )


def _touch_file(repo_root, rel_path, mtime_epoch):
    full = repo_root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# test\n", encoding="utf-8")
    os.utime(full, (mtime_epoch, mtime_epoch))


def _write_uncommitted_log(agent_dir, recorded_paths):
    (agent_dir / "session" / "uncommitted-edits.jsonl").write_text(
        "\n".join(json.dumps({"file": p}) for p in recorded_paths),
        encoding="utf-8",
    )


def _epoch_to_iso(ep):
    return datetime.datetime.fromtimestamp(ep).isoformat()


# ── Cases ────────────────────────────────────────────────────────────────────


def test_no_crash_when_self_in_flight_null(tmp_path):
    """Case 1 (graceful): filter_paths runs without raising when the self agent's
    team-state entry exists but in_flight is explicitly null (the post-
    clear-in-flight state). Returns a well-formed (kept, dropped) tuple."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {"in_flight": None},  # cleared by team-state-clear-in-flight
            "bravo": {"in_flight": {"goal_id": "g-bravo", "claimed_at": _epoch_to_iso(base + 60)}},
        },
    )
    files = ["core/scripts/neutral-a.py", "core/config/neutral-b.yaml"]
    for f in files:
        _touch_file(project_root, f, base + 30)

    kept, dropped = filter_paths(files, "zeta", str(project_root), str(world))

    assert isinstance(kept, list) and isinstance(dropped, list), (
        f"filter_paths must return (list, list); got kept={type(kept)}, "
        f"dropped={type(dropped)}")
    # Every entry must be accounted for exactly once across kept+dropped.
    assert len(kept) + len(dropped) == len(files), (
        f"path accounting leak: {len(kept)} kept + {len(dropped)} dropped "
        f"!= {len(files)} input")


def test_source2_uncommitted_log_drops_partner_when_self_null(tmp_path):
    """Case 2 (Source 2 robust): a partner file recorded in the partner's
    uncommitted-edits.jsonl is STILL dropped when self.in_flight=null. Source 2
    never consults self_claimed_at, so it is unaffected by the cleared claim."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(world, {"zeta": {"in_flight": None}})  # no self anchor

    partner_logged = "core/scripts/bravo-wip.py"
    _touch_file(project_root, partner_logged, base + 500)
    _write_uncommitted_log(project_root / "agents" / "bravo", [partner_logged])

    kept, dropped = filter_paths([partner_logged], "zeta", str(project_root), str(world))

    assert kept == [], f"Source 2 must drop partner-logged file; got kept={kept}"
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "partner-uncommitted-log"
    assert dropped[0]["owner"] == "bravo"


def test_source1_concurrent_partner_drops_when_self_null(tmp_path):
    """Case 3 (Source 1 robust): a file whose mtime is at/after a partner's
    in_flight claim is STILL dropped as concurrent-partner when self.in_flight=
    null. With self_claimed_at=0, skip_concurrent is False, so Source 1 fires."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {"in_flight": None},
            "bravo": {"in_flight": {"goal_id": "g-bravo", "claimed_at": _epoch_to_iso(base + 60)}},
        },
    )
    partner_concurrent = "core/scripts/bravo-concurrent.py"
    _touch_file(project_root, partner_concurrent, base + 90)  # after bravo's claim

    kept, dropped = filter_paths([partner_concurrent], "zeta", str(project_root), str(world))

    assert kept == [], f"Source 1 must drop concurrent-partner file; got kept={kept}"
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "concurrent-partner"
    assert dropped[0]["owner"] == "bravo"


def test_source3_gap_old_mtime_partner_kept_when_self_null(tmp_path):
    """Case 4 (documented Source 3 gap): a partner file with an OLD mtime, NOT
    in any uncommitted log, with NO concurrent partner claim, is KEPT when
    self.in_flight=null — because Source 3 (pre-claim-mtime) requires
    self_claimed_at>0 and is therefore disabled.

    This is the fail-open limitation, NOT a desired outcome. The same file is
    correctly dropped when self HAS an in_flight claim (see
    test_post_state_update_attribution.py::test_pre_claim_mtime_catches_partner_wip).
    Tracked by g-115-1154 (root-cause investigate) + g-115-1178 (gate-scope fix)
    + g-115-1180 (Source 2 path normalization). If a fix lands that closes the
    gap, update this case to assert the file is dropped."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    # Self in_flight cleared; partner has NO in_flight claim (stranded prior-session
    # WIP shape) → partner_epochs empty → Source 1 cannot fire either.
    _write_team_state(world, {"zeta": {"in_flight": None}, "bravo": {"in_flight": None}})

    partner_stranded = "core/scripts/bravo-stranded.py"
    _touch_file(project_root, partner_stranded, base)  # old mtime, no other signal

    kept, dropped = filter_paths([partner_stranded], "zeta", str(project_root), str(world))

    assert kept == [partner_stranded], (
        f"Source 3 gap: old-mtime partner file is KEPT when self.in_flight=null "
        f"(fail-open). got kept={kept}, dropped={dropped}")
    assert dropped == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
