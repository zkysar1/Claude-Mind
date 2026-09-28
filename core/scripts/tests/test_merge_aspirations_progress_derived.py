#!/usr/bin/env python3
"""merge_aspirations RE-DERIVES an aspiration's `progress` from the merged
record instead of carrying one side's aggregate (g-306-532).

THE DEFECT. _merge_aspiration_record builds the merged record on the base-pick
winner (newer last_selected, content tiebreak on a tie) and unions `goals` from
BOTH sides. Before this fix `progress` stayed the winner's. But progress is
DERIVED: aspirations.recompute_progress rewrites it from goals + archived_census
+ initial_goal_count on every write. So a side that won the base pick landed its
aggregate beside unioned goals that derive a different value. Measured by
replaying fcefb5bed8's inputs for agents/alpha/aspirations.jsonl (base
24772cd7d1, ours f62f467151, theirs 367bb9ee6f): asp-001 completed_goals came
out 375 while the merged goals derive 377. Both sides tied on last_selected
2026-09-27T10:33:24, so the content tiebreak picked theirs.

EXPECTED VALUES ARE READ FROM THE WRITER, never restated (guard-1220): every
assertion compares against aspirations.recompute_progress run over the merged
record. Every test also asserts that its adverse case is REAL, i.e. that the
side-picked value differs from the derived one, so it cannot pass vacuously if
the fixture stops exercising the defect (guard-2903, guard-3134).
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys

import pytest

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import _goal_census  # noqa: E402
import coordination_merge  # noqa: E402
from coordination_merge import merge_aspirations  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "aspirations_module", os.path.join(_SCRIPTS, "aspirations.py"))
asp_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asp_mod)

_DRIVER_PY = os.path.join(_SCRIPTS, "git-merge-ayoai-ledger.py")
_dspec = importlib.util.spec_from_file_location("git_merge_ayoai_ledger", _DRIVER_PY)
drv = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(drv)


def blob(*recs):
    return ("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs)).encode()


def lines(data):
    return [json.loads(x) for x in data.decode().splitlines() if x.strip()]


def written(rec):
    """What the WRITER would store as progress for this record."""
    r = copy.deepcopy(rec)
    asp_mod.recompute_progress(r)
    return r["progress"]


def asp(last_selected, goals, progress=True, **extra):
    rec = {"id": "asp-900", "title": "t", "status": "active",
           "last_selected": last_selected, "goals": goals, **extra}
    if progress:
        rec["progress"] = written(rec)
    return rec


def g(n, status, **extra):
    return {"id": f"g-900-{n:02d}", "title": f"goal {n}", "status": status,
            "created_at": f"2026-09-01T00:00:{n:02d}", **extra}


def test_stale_side_winning_the_base_pick_is_rederived():
    # A added a completed goal that B never saw; B won the base pick.
    a = asp("2026-09-27T10:00:00", [g(1, "completed"), g(2, "pending"), g(3, "completed")])
    b = asp("2026-09-27T11:00:00", [g(1, "completed"), g(2, "pending")])
    (m,) = lines(merge_aspirations(blob(a), blob(b)))
    assert len(m["goals"]) == 3, "fixture must union a goal only A had"
    assert b["progress"] != written(m), "adverse case not exercised"
    assert m["progress"] == written(m)
    assert merge_aspirations(blob(a), blob(b)) == merge_aspirations(blob(b), blob(a))


def test_one_sided_progress_is_rederived_not_carried():
    # The winner never carried progress; the loser's is stale for the union.
    a = asp("2026-09-27T10:00:00", [g(1, "completed"), g(2, "completed")])
    a["progress"] = {"completed_goals": 0, "total_goals": 1,
                     "recurring_goals": 0, "fan_out_ratio": None}
    b = asp("2026-09-27T11:00:00", [g(1, "completed")], progress=False)
    (m,) = lines(merge_aspirations(blob(a), blob(b)))
    assert a["progress"] != written(m), "adverse case not exercised"
    assert m["progress"] == written(m)
    assert merge_aspirations(blob(a), blob(b)) == merge_aspirations(blob(b), blob(a))


def test_a_record_without_progress_gains_none():
    a = asp("2026-09-27T10:00:00", [g(1, "completed")], progress=False)
    b = asp("2026-09-27T11:00:00", [g(1, "completed"), g(2, "pending")], progress=False)
    (m,) = lines(merge_aspirations(blob(a), blob(b)))
    assert "progress" not in m
    # Positive control (guard-3134): the SAME pair with progress keeps the key,
    # so the absence above is the rule, not a path that never emits it.
    (mc,) = lines(merge_aspirations(blob(asp(a["last_selected"], a["goals"])),
                                    blob(asp(b["last_selected"], b["goals"]))))
    assert "progress" in mc


def test_non_record_goal_fragment_does_not_break_the_merge():
    # _merge_goals passes non-dict fragments through; deriving must not raise.
    a = asp("2026-09-27T10:00:00", [g(1, "completed")])
    b = asp("2026-09-27T11:00:00", [g(1, "completed")])
    a["goals"].append("g-900-legacy-ref")
    (m,) = lines(merge_aspirations(blob(a), blob(b)))
    assert "g-900-legacy-ref" in m["goals"]
    assert m["progress"] == _goal_census.derive_progress(m)


def test_merge_calls_the_writer_body_not_a_copy():
    # No third transcription of the dual mirror: the merge and the CLI writer
    # share ONE function object (the daemon mirror is pinned elsewhere).
    assert coordination_merge._derive_progress is _goal_census.derive_progress
    assert asp_mod._derive_progress is _goal_census.derive_progress


_REPO = os.path.dirname(os.path.dirname(_SCRIPTS))
_PATH = "agents/alpha/aspirations.jsonl"
_FCEFB = ("24772cd7d1", "f62f467151", "367bb9ee6f")  # base, ours, theirs


def _have_commits():
    return all(subprocess.run(["git", "-C", _REPO, "cat-file", "-e", f"{s}^{{commit}}"],
                              capture_output=True).returncode == 0 for s in _FCEFB)


@pytest.mark.skipif(not _have_commits(), reason="fcefb5bed8 merge inputs not in this clone")
def test_real_fcefb5bed8_merge_derives_asp001_progress():
    def show(rev):
        return subprocess.run(["git", "-C", _REPO, "show", f"{rev}:{_PATH}"],
                              capture_output=True, check=True).stdout

    base, ours, theirs = (show(r) for r in _FCEFB)
    merged = drv.merge_bytes(_PATH, ours, theirs, base)
    (m,) = [r for r in lines(merged) if r["id"] == "asp-001"]
    (t,) = [r for r in lines(theirs) if r["id"] == "asp-001"]
    assert t["progress"] != written(m), "the stale-aggregate case is gone from the inputs"
    assert m["progress"] == written(m)
    assert merged == drv.merge_bytes(_PATH, theirs, ours, base)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
