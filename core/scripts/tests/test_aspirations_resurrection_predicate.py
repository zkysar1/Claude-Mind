"""_aspirations_resurrection — the ONE predicate behind the daemon's archive-sweep
reconcile and the read-only /verify-learning scan (goal-completion audit,
2026-08-16). Cases mirror mind_api/tests/test_runtime_aspirations_archive_sweep_resurrection.py
so the detector and the remedy are pinned to the same verdicts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import _aspirations_resurrection as R  # noqa: E402


def _asp(aid, status, goals, **extra):
    d = {"id": aid, "status": status, "goals": goals}
    d.update(extra)
    return d


def _g(gid, status, **kw):
    d = {"id": gid, "status": status}
    d.update(kw)
    return d


ARCH = [_asp("asp-xw-1", "retired", [_g("g-xw-1-01", "skipped", outcome_note="stub")],
             retired_at="2026-08-10"),
        _asp("asp-328", "completed", [_g("g-328-01", "completed"), _g("g-328-02", "skipped")],
             completed_at="2026-07-12"),
        _asp("asp-007", "retired", [_g("g-007-01", "pending")], retired_at="2026-08-01"),
        _asp("asp-009", "active", [_g("g-009-01", "completed")])]


def test_pristine_resurrected_copy_is_flagged_and_would_rearchive():
    live = [_asp("asp-xw-1", "active", [_g("g-xw-1-01", "pending")])]
    found = R.find_resurrected(live, ARCH)
    assert found == [{"asp_id": "asp-xw-1", "arch_status": "retired", "stamp": "2026-08-10",
                      "goal_ids": ["g-xw-1-01"], "post_archive_work": False,
                      "would_rearchive": True}]


def test_post_archive_work_keeps_the_aspiration_but_flags_the_stale_goal():
    live = [_asp("asp-328", "active", [_g("g-328-02", "pending"), _g("g-328-36", "pending")])]
    found = R.find_resurrected(live, ARCH)
    assert len(found) == 1
    assert found[0]["goal_ids"] == ["g-328-02"]
    assert found[0]["post_archive_work"] is True and found[0]["would_rearchive"] is False


def test_claimed_or_newer_live_goal_is_not_a_resurrection():
    live = [_asp("asp-xw-1", "active", [_g("g-xw-1-01", "pending", claimed_by="echo")])]
    assert R.find_resurrected(live, ARCH) == []
    live = [_asp("asp-xw-1", "active", [_g("g-xw-1-01", "pending", last_modified="2026-08-12T00:00:00")])]
    assert R.find_resurrected(live, ARCH) == []
    # same-day edit is NOT newer than the stamp -> still a resurrection
    live = [_asp("asp-xw-1", "active", [_g("g-xw-1-01", "pending", last_modified="2026-08-10T23:59:59")])]
    assert R.find_resurrected(live, ARCH)[0]["goal_ids"] == ["g-xw-1-01"]


def test_archive_holding_the_goal_open_or_the_aspiration_active_is_no_resurrection():
    live = [_asp("asp-007", "active", [_g("g-007-01", "pending")]),
            _asp("asp-009", "active", [_g("g-009-01", "pending")]),
            _asp("asp-new", "active", [_g("g-new-01", "pending")])]
    assert R.find_resurrected(live, ARCH) == []


def test_terminal_live_goal_and_recurring_goal_are_skipped():
    live = [_asp("asp-xw-1", "active", [_g("g-xw-1-01", "completed")])]
    assert R.find_resurrected(live, ARCH) == []
    arch = [_asp("asp-r", "retired", [_g("g-r-01", "skipped")], retired_at="2026-08-01")]
    live = [_asp("asp-r", "active", [_g("g-r-01", "pending", recurring=True)])]
    assert R.find_resurrected(live, arch) == []


def test_archive_by_id_last_row_wins_and_stamp_prefers_retired_at():
    rows = [_asp("a", "completed", [], completed_at="2026-01-01"),
            _asp("a", "retired", [], retired_at="2026-02-02", completed_at=None)]
    by = R.archive_by_id(rows)
    assert by["a"]["status"] == "retired"
    assert R.archive_terminal_stamp(by["a"]) == "2026-02-02"
    assert R.archive_terminal_stamp({"archived_at": "2026-03-03T10:00:00"}) == "2026-03-03"
    assert R.archive_terminal_stamp({}) == ""


def test_scan_cli_skips_loudly_when_stores_are_unreadable(tmp_path):
    # point the scan at a wrapper that returns nothing -> SKIP rc=2, never PASS
    fake = tmp_path / "aspirations-read.sh"
    fake.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    scan = tmp_path / "aspirations-resurrection-scan.py"
    scan.write_text((SCRIPTS / "aspirations-resurrection-scan.py").read_text(encoding="utf-8"),
                    encoding="utf-8")
    for helper in ("_aspirations_resurrection.py", "_goal_census.py", "_runtime_bash.py"):
        (tmp_path / helper).write_text((SCRIPTS / helper).read_text(encoding="utf-8"),
                                       encoding="utf-8")
    out = subprocess.run([sys.executable, str(scan), "--json"], capture_output=True, text=True)
    assert out.returncode == 2, out.stderr
    assert json.loads(out.stdout)["verdict"] == "SKIP"
