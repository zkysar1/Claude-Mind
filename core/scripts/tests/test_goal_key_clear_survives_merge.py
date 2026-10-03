"""test_goal_key_clear_survives_merge.py — a goal key that update_goal's cascades
CLEAR stays cleared through a goal merge, on both merge routes and in both
argument orders (g-115-11591).

THE DEFECT. The recurring=false cascade and the defer-clear cascade (daemon
aspirations_write.update_goal and its CLI mirror cmd_update_goal) and the CLI
terminal normalizer POPPED keys. coordination_merge._merge_goal keeps a key that
is present on only one side (g-115-5017), so a popped key came back from any
peer copy that still held it. lastAchievedAt, merged strictly-newer-wins with
None sorting oldest, came back from any peer value at all. A retired recurring
goal then carried the rb-295 shape (completed, recurring false, interval_hours
and lastAchievedAt set) that the recover-recurring sweep reopens.

THE FIX has two halves (rb-5493). The writers clear to None instead of popping,
and _merge_goal's newer-fields tier lets a clear on the base-pick winner hold over
an older value. This file pins the RESOLVER half through merge_aspirations (the
own-cloud route) and the git ledger driver's merge_bytes (the git route, both the
world queue and the delete-aware agent queue). It also pins the CLI normalizer
and the three interval readers that must read a cleared value as absent. The
daemon writer half is pinned in mind_api/tests (test_runtime_update_goal_cascade,
test_wrapper_aspirations_update_goal). A positive control proves a POPPED key
still comes back, so these assertions cannot pass vacuously.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_merge as cm  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ledger = _load("git_merge_ayoai_ledger_for_clear_tests", "git-merge-ayoai-ledger.py")
_ASP = None


def _asp():
    """aspirations.py, loaded on first use: it resolves WORLD_DIR at import, so a
    module-level load would fail COLLECTION of the merge tests too on a checkout
    where the world is not configured."""
    global _ASP
    if _ASP is None:
        _ASP = _load("aspirations_for_clear_tests", "aspirations.py")
    return _ASP

T_ACHIEVED = "2026-09-28T07:55:00"   # the peer's last achievement
T_PEER = "2026-09-28T08:00:00"       # the peer copy's last write: before the clear
T_CLEAR = "2026-09-28T09:04:00"      # the clearing write
T_LATER = "2026-09-28T10:00:00"      # after the clear

DEFER_KEYS = ("deferred_until", "blocker_ref", "blocked_since")
RECURRING_KEYS = ("interval_hours", "lastAchievedAt")


def _goal(**kw) -> dict:
    g = {"id": "g-1-1", "alloc_nonce": "nonce-g-1-1", "title": "hourly monitor",
         "created_at": "2026-09-01T00:00:00", "status": "pending",
         "recurring": True, "interval_hours": 1.0, "lastAchievedAt": T_ACHIEVED,
         "last_modified": T_PEER}
    g.update(kw)
    return g


def _blob(goal: dict) -> bytes:
    asp = {"id": "asp-1", "status": "active", "goals": [goal]}
    return (json.dumps(asp, ensure_ascii=True) + "\n").encode()


def _merged_asp(blob: bytes) -> dict:
    recs = [json.loads(ln) for ln in blob.decode().splitlines() if ln.strip()]
    assert len(recs) == 1, recs
    return recs[0]


def _merged_goal(blob: bytes) -> dict:
    goals = _merged_asp(blob)["goals"]
    assert len(goals) == 1, goals
    return goals[0]


def _all_routes(writer: bytes, peer: bytes):
    """Every route and argument order, as (label, merged bytes). The git driver
    gets the peer copy as the merge base: the peer is unchanged since the base,
    which is the production trace in g-115-11591's description."""
    yield "own-cloud merge(writer, peer)", cm.merge_aspirations(writer, peer)
    yield "own-cloud merge(peer, writer)", cm.merge_aspirations(peer, writer)
    for path in ("aspirations.jsonl", "agents/alpha/aspirations.jsonl"):
        yield (f"git {path} ours=writer",
               ledger.merge_bytes(path, writer, peer, base=peer))
        yield (f"git {path} ours=peer",
               ledger.merge_bytes(path, peer, writer, base=peer))


def _retired(**kw) -> dict:
    """The record the recurring=false cascade leaves: cleared, not popped."""
    g = _goal(status="completed", recurring=False, interval_hours=None,
              lastAchievedAt=None, completed_at=T_CLEAR, last_modified=T_CLEAR)
    g.update(kw)
    return g


def test_recurring_retirement_survives_merge_on_every_route():
    writer, peer = _blob(_retired()), _blob(_goal())
    for label, merged in _all_routes(writer, peer):
        g = _merged_goal(merged)
        assert g["recurring"] is False, label
        assert g["status"] == "completed", label
        for key in RECURRING_KEYS:
            assert key in g and g[key] is None, f"{label}: {key}={g.get(key)!r}"
        assert _asp().find_shape_recurring_corrupted(_merged_asp(merged)) == [], label
    assert cm.merge_aspirations(writer, peer) == cm.merge_aspirations(peer, writer)


def test_defer_clear_survives_merge_on_every_route():
    peer = _blob(_goal(recurring=False, defer_reason="precondition_unmet: partner leg",
                       defer_reason_set_at=T_PEER,
                       blocker_ref={"type": "dependency", "external_id": "g-1-2"},
                       deferred_until="2026-09-29T00:00:00", blocked_since=T_PEER))
    writer = _blob(_goal(recurring=False, defer_reason=None, defer_reason_set_at=None,
                         blocker_ref=None, deferred_until=None, blocked_since=None,
                         last_modified=T_CLEAR))
    for label, merged in _all_routes(writer, peer):
        g = _merged_goal(merged)
        for key in ("defer_reason", "defer_reason_set_at") + DEFER_KEYS:
            assert key in g and g[key] is None, f"{label}: {key}={g.get(key)!r}"
    assert cm.merge_aspirations(writer, peer) == cm.merge_aspirations(peer, writer)


def test_positive_control_a_popped_key_still_comes_back():
    """The pre-fix writer shape: the same records with the keys POPPED. Every
    route restores them, which is the defect, so the tests above discriminate."""
    retired = _retired()
    for key in RECURRING_KEYS:
        del retired[key]
    for label, merged in _all_routes(_blob(retired), _blob(_goal())):
        g = _merged_goal(merged)
        assert g.get("interval_hours") == 1.0, label
        assert g.get("lastAchievedAt") == T_ACHIEVED, label
        assert _asp().find_shape_recurring_corrupted(_merged_asp(merged)), label


def test_an_achievement_newer_than_the_clear_still_wins():
    """A close does not stamp last_modified, so a peer can hold an achievement
    newer than the clearing write while its own last_modified is older. The newer
    event wins. interval_hours stays cleared, so the rb-295 shape cannot form."""
    writer = _blob(_retired())
    peer = _blob(_goal(lastAchievedAt=T_LATER))
    for label, merged in _all_routes(writer, peer):
        g = _merged_goal(merged)
        assert g["lastAchievedAt"] == T_LATER, label
        assert g["interval_hours"] is None, label
        assert _asp().find_shape_recurring_corrupted(_merged_asp(merged)) == [], label


def test_a_clear_on_the_losing_record_defers_to_the_winner_snapshot():
    """The peer wrote after the clear (an unrelated edit to its stale copy), so
    the peer's record wins the base-pick whole. The clear does not reach into it:
    the merged record stays one coherent snapshot."""
    writer = _blob(_retired())
    peer = _blob(_goal(last_modified=T_LATER))
    for label, merged in _all_routes(writer, peer):
        g = _merged_goal(merged)
        assert g["recurring"] is True, label
        assert g["interval_hours"] == 1.0, label
        assert g["lastAchievedAt"] == T_ACHIEVED, label


def test_one_sided_added_keys_are_still_preserved():
    """ / : a key ADDED on only one side survives, beside
    the clears. completed_date and completed_by come from the close path and
    defer_reason from the defer path, none of which stamps last_modified."""
    writer = _blob(_retired())
    peer = _blob(_goal(completed_date="2026-09-28", completed_by="alpha",
                       defer_reason="human_blocked: owner ruling"))
    for label, merged in _all_routes(writer, peer):
        g = _merged_goal(merged)
        assert g["completed_date"] == "2026-09-28", label
        assert g["completed_by"] == "alpha", label
        assert g["defer_reason"] == "human_blocked: owner ruling", label
        for key in RECURRING_KEYS:
            assert g[key] is None, f"{label}: {key}"


def test_cli_normalizer_clears_present_keys_and_adds_none():
    present = {"id": "g-1-1", "status": "completed", "completed_at": T_CLEAR,
               "deferred_until": "2026-09-29", "blocked_since": T_PEER,
               "blocker_ref": {"type": "dependency", "external_id": "g-1-2"}}
    _asp()._normalize_terminal_goal(present)
    for key in DEFER_KEYS:
        assert key in present and present[key] is None, key
    absent = {"id": "g-1-2", "status": "completed", "completed_at": T_CLEAR}
    _asp()._normalize_terminal_goal(absent)
    for key in DEFER_KEYS:
        assert key not in absent, key


def test_interval_readers_read_a_cleared_value_as_absent():
    gs = importlib.import_module("goal-selector")
    cadence = importlib.import_module("cadence_signals")
    realloc = importlib.import_module("gates.reallocation_exempt")
    cases = (({"interval_hours": None}, 24),
             ({"interval_hours": None, "remind_days": 2}, 48),
             ({"interval_hours": 6}, 6))
    for goal, want in cases:
        assert gs.get_interval_hours(goal) == want, goal
        assert cadence._interval_hours(goal) == float(want), goal
        assert realloc._interval_hours(goal) == want, goal


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
